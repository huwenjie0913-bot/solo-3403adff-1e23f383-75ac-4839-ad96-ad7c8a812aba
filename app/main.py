"""FastAPI 入口：提交校核、按批次检索、批次重审/配方版次并列查看。"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from . import storage
from .compare import compare_reports
from .engine import evaluate
from .rules import DEFAULT_RULE_VERSION, RULE_VERSIONS
from .schemas import CompareResponse, EvaluateRequest, EvaluationReport


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield


app = FastAPI(
    title="热处理炉批次曲线校核 API",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "提交炉区测点、带采样时刻的温度读数、材料阶段与工艺带/速率/超调/"
        "保温/容差参数，逐阶段校核速率、保温带时刻、区间均匀性与超温累计，"
        "每条判定附引用读数；结果落 SQLite，支持批次重审与配方版次并列查看。"
    ),
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rules")
def list_rules():
    """规则版本目录（缺省取最新版）。"""
    return {
        "default": DEFAULT_RULE_VERSION,
        "versions": [{"version": v, **meta} for v, meta in RULE_VERSIONS.items()],
    }


@app.post("/evaluate", response_model=EvaluationReport, status_code=201)
def submit_evaluation(req: EvaluateRequest):
    """提交一批读数进行校核；INPUT_ERROR 也返回 201 并落库（结构错误才 422）。"""
    try:
        report = evaluate(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    evaluation_id = storage.save_evaluation(req, report)
    return EvaluationReport.model_validate(
        {**report.model_dump(), "evaluation_id": evaluation_id}
    )


@app.get("/evaluations/{evaluation_id}", response_model=EvaluationReport)
def get_evaluation(evaluation_id: int):
    row = storage.get_evaluation(evaluation_id)
    if row is None:
        raise HTTPException(404, f"校核记录 {evaluation_id} 不存在")
    return row


@app.get("/batches/{batch_no}/evaluations", response_model=list[EvaluationReport])
def list_batch_evaluations(batch_no: str):
    """按批次号列出全部校核（重审历史），按提交先后排序。"""
    return storage.list_by_batch(batch_no)


@app.get("/batches/{batch_no}/latest", response_model=EvaluationReport)
def latest_batch_evaluation(batch_no: str):
    row = storage.latest_by_batch(batch_no)
    if row is None:
        raise HTTPException(404, f"批次 {batch_no} 无校核记录")
    return row


@app.get("/compare", response_model=CompareResponse)
def compare(
    a: int = Query(..., description="校核记录 A 的 id"),
    b: Optional[int] = Query(None, description="校核记录 B 的 id；缺省取 A 同批次上一条"),
):
    """并列查看两条判定（批次重审对比 / 配方版次对比）。"""
    row_a = storage.get_evaluation(a)
    if row_a is None:
        raise HTTPException(404, f"校核记录 {a} 不存在")
    if b is None:
        history = storage.list_by_batch(row_a["batch_no"])
        prev = [r for r in history if r["evaluation_id"] < a]
        if not prev:
            raise HTTPException(404, f"记录 {a} 是批次 {row_a['batch_no']} 的首条，无可对比的前次校核")
        row_b = prev[-1]
    else:
        row_b = storage.get_evaluation(b)
        if row_b is None:
            raise HTTPException(404, f"校核记录 {b} 不存在")

    try:
        rep_a = EvaluationReport.model_validate(row_a)
        rep_b = EvaluationReport.model_validate(row_b)
    except ValidationError as exc:
        raise HTTPException(500, f"库存报告反序列化失败：{exc}") from exc

    if rep_b.batch_no != rep_a.batch_no:
        raise HTTPException(
            422, f"记录分属不同批次：{rep_a.batch_no} 与 {rep_b.batch_no}，仅限同批次并列查看"
        )
    return compare_reports(rep_a, rep_b)
