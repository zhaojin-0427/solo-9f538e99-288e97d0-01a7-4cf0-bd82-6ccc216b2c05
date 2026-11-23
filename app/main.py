"""FastAPI surface for coating-thickness reporting and P95 queries."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Iterator

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr

from . import __version__, db
from .domain import Bucket, ReconciliationError, TargetSpec, ValidationError
from .service import (
    CoilState,
    DayTableState,
    LineP95,
    get_coil,
    get_day_table,
    query_p95,
    refine_bucket,
    submit_day_table,
    submit_day_tables_and_reports,
    submit_reports,
)

DOCS = "涂层厚度 P95 查询服务"


class BucketIn(BaseModel):
    lo: StrictInt = Field(description="闭区间下界，整数微米（含）")
    hi: StrictInt = Field(description="闭区间上界，整数微米（含）")
    count: StrictInt = Field(gt=0, description="落在该档内的测点数，正整数")

    def to_domain(self) -> Bucket:
        return Bucket(lo=self.lo, hi=self.hi, count=self.count)


class TargetIn(BaseModel):
    lo: StrictInt = Field(description="原档下界，整数微米（含）")
    hi: StrictInt = Field(description="原档上界，整数微米（含）")
    count: StrictInt | None = Field(
        default=None,
        gt=0,
        description="可选：原档计数。给出时必须与当前报送一致，用于防误改。",
    )

    def to_domain(self) -> TargetSpec:
        return TargetSpec(lo=self.lo, hi=self.hi, count=self.count)


class BucketOut(BaseModel):
    lo: int
    hi: int
    count: int


class ReportIn(BaseModel):
    line_id: StrictStr = Field(min_length=1, description="产线编号")
    production_day: date = Field(description="生产日，YYYY-MM-DD")
    coil_id: StrictStr = Field(min_length=1, description="卷号")
    buckets: list[BucketIn] = Field(
        min_length=1,
        description="日汇总分档计数。同一卷内的闭区间必须互不重叠，计数为正整数。",
    )


class BulkReportIn(BaseModel):
    reports: list[ReportIn] = Field(
        min_length=1, description="多卷日汇总；整批要么全部生效，要么全部不生效。"
    )



class RefineIn(BaseModel):
    production_day: date = Field(description="该卷的生产日")
    target: TargetIn = Field(description="要被替换的当前档，按闭区间定位")
    children: list[BucketIn] = Field(
        min_length=1,
        description=(
            "更窄的子档：须完全落在原档内、彼此互不重叠，"
            "计数之和必须等于原档计数。"
        ),
    )


class CoilOut(BaseModel):
    line_id: str
    coil_id: str
    production_day: date
    version: int = Field(description="该日汇总每成功提交一次加一；失败不改变")
    buckets: list[BucketOut]


class DayTableIn(BaseModel):
    line_id: StrictStr = Field(min_length=1, description="产线编号")
    production_day: date = Field(description="生产日，YYYY-MM-DD")
    buckets: list[BucketIn] = Field(
        min_length=1,
        description=(
            "仪器汇成的整日分档计数，沿用卷报的闭区间整数微米分档格式。"
            "同一日内各档互不重叠，计数为正整数。"
        ),
    )


class DayTableOut(BaseModel):
    line_id: str
    production_day: date
    version: int = Field(description="该产线该日仪器日表每成功提交一次加一；失败不改变")
    buckets: list[BucketOut]


class DayTablesAndReportsIn(BaseModel):
    """Instrument day tables plus coil reports, written and checked together.

    Lets a day table and its coil reports arrive at the same time and still be
    accepted only when the resulting effective data pairs completely.  At least
    one of the two lists must be non-empty; an empty request is rejected by the
    service layer.
    """

    day_tables: list[DayTableIn] = Field(
        default_factory=list, description="要登记或替换的仪器日表（可空）"
    )
    reports: list[ReportIn] = Field(
        default_factory=list, description="要报送的卷日汇总（可空）"
    )


class DayTablesAndReportsOut(BaseModel):
    day_tables: list[DayTableOut]
    reports: list[CoilOut]


class DayMismatchOut(BaseModel):
    line_id: str
    production_day: date
    instrument_total: int = Field(description="该日仪器日表的测点总数")
    coil_total: int = Field(description="该日全部卷报的测点总数")
    instrument_unpaired: int = Field(
        description="配不齐时，仪器侧最少配不上的测点数（最大配对下的剩余）"
    )
    coil_unpaired: int = Field(
        description="配不齐时，卷报侧最少配不上的测点数（最大配对下的剩余）"
    )


class ReconciliationDetail(BaseModel):
    message: str = Field(description="人类可读的拒收原因")
    mismatches: list[DayMismatchOut] = Field(
        default_factory=list, description="每条配不齐的产线与生产日"
    )


class ReconciliationErrorOut(BaseModel):
    detail: ReconciliationDetail


# Documented 422 shape for requests rejected because points cannot be paired.
RECONCILIATION_RESPONSE = {
    422: {
        "model": ReconciliationErrorOut,
        "description": (
            "有产线生产日配不齐，整次请求退回。detail.mismatches 列出每一天及"
            "两边各自最少配不上的测点数；其他校验错误的 422 结构可能不同。"
        ),
    }
}


class CandidateBucket(BaseModel):
    coil_id: str = Field(description="需要补细分数据的卷号")
    production_day: date
    lo: int
    hi: int
    count: int


class P95Out(BaseModel):
    line_id: str
    start: date
    end: date
    has_data: bool = Field(description="该产线在该日期范围内是否有任何测点")
    measurements: int | None = Field(
        default=None, description="测点总数（各档计数之和，不展开测点）"
    )
    rank: int | None = Field(
        default=None, description="P95 秩：ceil(0.95 × 测点数)，从 1 开始"
    )
    p95_min: int | None = Field(default=None, description="P95 的最小可能值")
    p95_max: int | None = Field(default=None, description="P95 的最大可能值")
    candidate_buckets: list[CandidateBucket] = Field(
        default_factory=list,
        description=(
            "两端不同时，与 [p95_min, p95_max] 交集至少包含两个整数微米值的分档；"
            "这些卷值得补细分数据。两端相同时为空。"
        ),
    )
    coil_ids: list[str] = Field(
        default_factory=list,
        description="candidate_buckets 中出现的卷号（去重、保持顺序）",
    )


def _coil_out(state: CoilState) -> CoilOut:
    return CoilOut(
        line_id=state.line_id,
        coil_id=state.coil_id,
        production_day=state.production_day,
        version=state.version,
        buckets=[BucketOut(lo=b.lo, hi=b.hi, count=b.count) for b in state.buckets],
    )


def _day_table_out(state: DayTableState) -> DayTableOut:
    return DayTableOut(
        line_id=state.line_id,
        production_day=state.production_day,
        version=state.version,
        buckets=[BucketOut(lo=b.lo, hi=b.hi, count=b.count) for b in state.buckets],
    )


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = request.app.state.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def create_app(database: str | None = None) -> FastAPI:
    database = database or db.database_path()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db(database)
        yield

    app = FastAPI(
        title=DOCS,
        description=(
            "按卷报送分档计数，用更窄子档细化；按产线与日期范围查询 P95 的"
            "最小/最大可能值，并列出需要补细分数据的卷与档。"
            "可按产线与生产日登记、替换、查阅仪器日表，并用它核对整日卷报："
            "两档只要有共同的整数微米值即可配对、每个点只配一次，两边都配得齐才收。"
            "P95 秩为第 ceil(0.95×测点数) 小的测值；只读日汇总（分档计数），"
            "不展开测点。"
        ),
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.connect = lambda: db.connect(database)

    @app.exception_handler(ReconciliationError)
    async def reconciliation_error(
        _: Request, exc: ReconciliationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": {
                    "message": str(exc),
                    "mismatches": [
                        {
                            "line_id": m.line_id,
                            "production_day": m.production_day.isoformat(),
                            "instrument_total": m.instrument_total,
                            "coil_total": m.coil_total,
                            "instrument_unpaired": m.instrument_unpaired,
                            "coil_unpaired": m.coil_unpaired,
                        }
                        for m in exc.mismatches
                    ],
                }
            },
        )

    @app.exception_handler(ValidationError)
    async def domain_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def request_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.errors()},
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health", summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/reports",
        response_model=CoilOut,
        status_code=status.HTTP_201_CREATED,
        summary="按卷号报送产线、生产日与分档计数",
        description=(
            "卷由 (产线, 生产日, 卷号) 定位。重复报送同一键会整体替换该日汇总；"
            "分档与日汇总在同一事务中生效，任何校验失败都不会写入。若该产线该日"
            "已有仪器日表，则用本次改完后的整日卷报复核，配不齐整次退回。"
        ),
        responses=RECONCILIATION_RESPONSE,
    )
    def post_report(payload: ReportIn, conn: Conn) -> CoilOut:
        state = submit_reports(
            conn,
            [
                (
                    payload.line_id,
                    payload.production_day,
                    payload.coil_id,
                    [b.to_domain() for b in payload.buckets],
                )
            ],
        )[0]
        return _coil_out(state)

    @app.post(
        "/reports/bulk",
        response_model=list[CoilOut],
        status_code=status.HTTP_201_CREATED,
        summary="批量报送多卷日汇总（整批原子生效）",
        description=(
            "适合一条产线一天上千卷的上报。整批在同一事务中生效：任一卷不合格，"
            "全部回滚，原数据不变。同一批内不得重复同一个 (产线, 生产日, 卷号)。"
            "涉及的产线生产日若已有仪器日表，则用本次改完后的整日卷报复核，"
            "任一天配不齐整批退回。"
        ),
        responses=RECONCILIATION_RESPONSE,
    )
    def post_bulk(
        payload: Annotated[BulkReportIn, Body()], conn: Conn
    ) -> list[CoilOut]:
        states = submit_reports(
            conn,
            [
                (
                    report.line_id,
                    report.production_day,
                    report.coil_id,
                    [b.to_domain() for b in report.buckets],
                )
                for report in payload.reports
            ],
        )
        return [_coil_out(state) for state in states]

    @app.post(
        "/day-tables/submit",
        response_model=DayTablesAndReportsOut,
        status_code=status.HTTP_201_CREATED,
        summary="同时提交仪器日表与卷报（同一事务，生效后必须配得齐）",
        description=(
            "仪器日表与卷报可一次提交，全部写入同一事务后再核对。这样即使日表与"
            "卷报同时到达，生效的数据也必须配得齐；任一产线生产日配不齐就整次退回，"
            "原日表、卷报与 P95 不变。"
        ),
        responses=RECONCILIATION_RESPONSE,
    )
    def post_day_tables_and_reports(
        payload: Annotated[DayTablesAndReportsIn, Body()], conn: Conn
    ) -> DayTablesAndReportsOut:
        tables, states = submit_day_tables_and_reports(
            conn,
            day_tables=[
                (
                    table.line_id,
                    table.production_day,
                    [b.to_domain() for b in table.buckets],
                )
                for table in payload.day_tables
            ],
            reports=[
                (
                    report.line_id,
                    report.production_day,
                    report.coil_id,
                    [b.to_domain() for b in report.buckets],
                )
                for report in payload.reports
            ],
        )
        return DayTablesAndReportsOut(
            day_tables=[_day_table_out(state) for state in tables],
            reports=[_coil_out(state) for state in states],
        )

    @app.post(
        "/reports/{line_id}/{coil_id}/refinements",
        response_model=CoilOut,
        summary="用更窄子档替换当前某一档",
        description=(
            "子档须落在原档内、彼此互不重叠，计数之和等于原档计数。新档位与日汇总"
            "在同一事务提交；任一条不满足则整体回滚，沿用原数据。若该产线该日已有"
            "仪器日表，细化后还须与整日卷报配得齐，否则整次退回。"
        ),
        responses=RECONCILIATION_RESPONSE,
    )
    def post_refinement(
        line_id: str, coil_id: str, payload: RefineIn, conn: Conn
    ) -> CoilOut:
        state = refine_bucket(
            conn,
            line_id=line_id,
            production_day=payload.production_day,
            coil_id=coil_id,
            target=payload.target.to_domain(),
            children=[b.to_domain() for b in payload.children],
        )
        return _coil_out(state)

    @app.get(
        "/reports/{line_id}/{coil_id}",
        response_model=CoilOut,
        summary="查看某卷某生产日当前的分档与版本",
    )
    def read_coil(
        line_id: str,
        coil_id: str,
        conn: Conn,
        production_day: Annotated[date, Query(description="生产日，YYYY-MM-DD")],
    ) -> CoilOut:
        try:
            state = get_coil(
                conn,
                line_id=line_id,
                production_day=production_day,
                coil_id=coil_id,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _coil_out(state)

    @app.post(
        "/day-tables",
        response_model=DayTableOut,
        status_code=status.HTTP_201_CREATED,
        summary="登记或替换某产线某生产日的仪器日表",
        description=(
            "仪器汇成的整日分档，沿用卷报的闭区间整数微米分档格式。"
            "写入后，该日仪器侧的全部测点必须能与该日全部卷报的测点一一配对；"
            "两档只要有共同的整数微米值即可配对，每个点只能配一次。"
            "配不齐则整次请求退回（422），并在 mismatches 中给出每条产线、"
            "每一天以及两边各自最少配不上的测点数。已存在的日表、卷报、P95 不受影响。"
        ),
        responses=RECONCILIATION_RESPONSE,
    )
    def post_day_table(payload: DayTableIn, conn: Conn) -> DayTableOut:
        state = submit_day_table(
            conn,
            line_id=payload.line_id,
            production_day=payload.production_day,
            buckets=[b.to_domain() for b in payload.buckets],
        )
        return _day_table_out(state)

    @app.get(
        "/day-tables/{line_id}",
        response_model=DayTableOut,
        summary="查看某产线某生产日的仪器日表",
    )
    def read_day_table(
        line_id: str,
        conn: Conn,
        production_day: Annotated[date, Query(description="生产日，YYYY-MM-DD")],
    ) -> DayTableOut:
        try:
            state = get_day_table(
                conn, line_id=line_id, production_day=production_day
            )
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _day_table_out(state)

    @app.get(
        "/p95",
        response_model=P95Out,
        summary="按产线与日期范围查询 P95 的最小/最大可能值",
        description=(
            "只读取日汇总（分档计数），按秩 ceil(0.95×N) 计算；下界取各档下界的"
            "加权第 k 小，上界取各档上界的加权第 k 小。两端与候选分档在同一只读"
            "事务快照中取得，即来自同一次提交。无测点时 has_data=false 且各值为 null。"
        ),
    )
    def read_p95(
        conn: Conn,
        line_id: Annotated[str, Query(min_length=1, description="产线编号")],
        start: Annotated[date, Query(description="起始生产日（含），YYYY-MM-DD")],
        end: Annotated[date, Query(description="结束生产日（含），YYYY-MM-DD")],
    ) -> P95Out:
        result: LineP95 = query_p95(conn, line_id=line_id, start=start, end=end)
        if not result.has_data:
            return P95Out(
                line_id=result.line_id,
                start=result.start,
                end=result.end,
                has_data=False,
            )
        candidates = [
            CandidateBucket(
                coil_id=candidate.coil_id,
                production_day=candidate.production_day,
                lo=candidate.bucket.lo,
                hi=candidate.bucket.hi,
                count=candidate.bucket.count,
            )
            for candidate in (result.candidates or ())
        ]
        coil_ids: list[str] = []
        for candidate in candidates:
            if candidate.coil_id not in coil_ids:
                coil_ids.append(candidate.coil_id)
        return P95Out(
            line_id=result.line_id,
            start=result.start,
            end=result.end,
            has_data=True,
            measurements=result.total,
            rank=result.rank,
            p95_min=result.minimum,
            p95_max=result.maximum,
            candidate_buckets=candidates,
            coil_ids=coil_ids,
        )

    return app


app = create_app()
