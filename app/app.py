"""Flask + SQLite 底图转换网页应用。

数据模型（SQLite）
==================
* ``projects``     一个底图转换任务（固定尺寸）
* ``snapshots``    每次成功/失败的转换记录；最新一条 status='ok' 的快照
                   即“上一份可下载的底图”，失败时不会被覆盖
* ``locks``        分米锁定值（沟口等），更正上传时继续沿用
* ``checks``       双路来水核查：绑定一份成功快照与两个来水格、一个沟口格，
                   保存备选高程测段，以及建立时一次算好的全部测段取值组合
* ``check_confirmations`` 核查内每个测段的确认取值（快照值 / 备选值）
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .dual import (
    MAX_SEGMENTS,
    decode_flows,
    effective_grid,
    mask_choices,
    parse_alternatives,
    precompute,
    select_mask,
    to_alternative_csv,
    verify_paths,
)
from .solver import (
    MAX_DIM,
    ValidationError,
    convert,
    flow_sets,
    parse_csv,
    to_csv,
)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "local_data", "drainage.db"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

ARROW = {(-1, 0): "↑", (1, 0): "↓", (0, -1): "←", (0, 1): "→"}


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc: object) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            rows       INTEGER NOT NULL,
            cols       INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind         TEXT NOT NULL,            -- initial / correction
            source_name  TEXT,
            input_cm     TEXT NOT NULL,            -- JSON 厘米格网
            error_limit  INTEGER NOT NULL,
            min_error    INTEGER,
            status       TEXT NOT NULL,            -- ok / tight / conflict
            grid_dm      TEXT,                     -- JSON 分米格网，失败时为 NULL
            errors       TEXT,                     -- JSON 逐格误差
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locks (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            row_idx    INTEGER NOT NULL,
            col_idx    INTEGER NOT NULL,
            dm_value   INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, row_idx, col_idx)
        );
        CREATE TABLE IF NOT EXISTS checks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            kind         TEXT NOT NULL DEFAULT 'dual',
            outlet_row   INTEGER NOT NULL,
            outlet_col   INTEGER NOT NULL,
            source_a_row INTEGER NOT NULL,
            source_a_col INTEGER NOT NULL,
            source_b_row INTEGER NOT NULL,
            source_b_col INTEGER NOT NULL,
            input_cm     TEXT NOT NULL,   -- 建立核查时快照的厘米格网（冻结副本）
            segments     TEXT NOT NULL,   -- JSON {测段名: {"r,c": 备选厘米}}
            precomputed  TEXT NOT NULL,   -- JSON 全部测段取值的流向/路线/建议
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS check_confirmations (
            check_id  INTEGER NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
            segment   TEXT NOT NULL,
            use_alt   INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (check_id, segment)
        );
        """
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_locks(conn: sqlite3.Connection, project_id: int) -> dict[tuple[int, int], int]:
    rows = conn.execute(
        "SELECT row_idx, col_idx, dm_value FROM locks WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return {(r["row_idx"], r["col_idx"]): r["dm_value"] for r in rows}


def _latest_ok_snapshot(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE project_id = ? AND status = 'ok' "
        "ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _latest_snapshot(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _read_upload(file_storage) -> str:
    if file_storage is None or not file_storage.filename:
        raise ValidationError("请选择要上传的 CSV 文件")
    raw = file_storage.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV 需为 UTF-8 编码文本") from exc


def _parse_error_limit(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError("最大允许误差必须是整数厘米") from exc
    if value < 0:
        raise ValidationError("最大允许误差不能为负数")
    return value


def _arrow_table(grid: list[list[int]]) -> list[list[str]]:
    return _arrow_table_from_flows(flow_sets(grid))


def _arrow_table_from_flows(flows: list[list[list[tuple[int, int]]]]) -> list[list[str]]:
    table: list[list[str]] = []
    for r, row in enumerate(flows):
        line = []
        for c, targets in enumerate(row):
            if not targets:
                line.append("·")
            else:
                line.append("".join(ARROW[(nr - r, nc - c)] for nr, nc in targets))
        table.append(line)
    return table


def _dashboard_context(conn: sqlite3.Connection, project: sqlite3.Row) -> dict:
    snaps = conn.execute(
        "SELECT * FROM snapshots WHERE project_id = ? ORDER BY id DESC LIMIT 10",
        (project["id"],),
    ).fetchall()
    lock_rows = conn.execute(
        "SELECT * FROM locks WHERE project_id = ? ORDER BY row_idx, col_idx",
        (project["id"],),
    ).fetchall()

    latest = _latest_snapshot(conn, project["id"])
    ok = _latest_ok_snapshot(conn, project["id"])
    # 可下载底图始终取最新成功快照；失败尝试不会覆盖它。
    source = ok if ok is not None else latest
    input_cm = json.loads(source["input_cm"])
    grid_dm = json.loads(source["grid_dm"]) if source["grid_dm"] else None
    errors = json.loads(source["errors"]) if source["errors"] else None

    ok_snapshots = conn.execute(
        "SELECT id, kind, source_name, created_at FROM snapshots "
        "WHERE project_id = ? AND status = 'ok' ORDER BY id DESC",
        (project["id"],),
    ).fetchall()
    check_rows = conn.execute(
        "SELECT c.*, s.kind AS snapshot_kind, s.created_at AS snapshot_created "
        "FROM checks c JOIN snapshots s ON s.id = c.snapshot_id "
        "WHERE c.project_id = ? ORDER BY c.id DESC",
        (project["id"],),
    ).fetchall()

    return {
        "project": project,
        "snapshots": snaps,
        "locks": lock_rows,
        "lock_cells": _load_locks(conn, project["id"]),
        "latest": latest,
        "downloadable": ok,
        "input_cm": input_cm,
        "grid_dm": grid_dm,
        "errors": errors,
        "flow_cm": _arrow_table(input_cm),
        "flow_dm": _arrow_table(grid_dm) if grid_dm is not None else None,
        "max_dim": MAX_DIM,
        "ok_snapshots": ok_snapshots,
        "checks": check_rows,
        "max_segments": MAX_SEGMENTS,
    }


def _segments_to_json(segments: dict[str, dict[tuple[int, int], int]]) -> str:
    return json.dumps(
        {
            name: [[r, c, value] for (r, c), value in sorted(cells.items())]
            for name, cells in segments.items()
        }
    )


def _segments_from_json(raw: str) -> dict[str, dict[tuple[int, int], int]]:
    return {
        name: {(r, c): value for r, c, value in rows}
        for name, rows in json.loads(raw).items()
    }


def _load_confirmations(conn: sqlite3.Connection, check_id: int) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT segment, use_alt FROM check_confirmations WHERE check_id = ?",
        (check_id,),
    ).fetchall()
    return {row["segment"]: bool(row["use_alt"]) for row in rows}


def _save_confirmation(
    conn: sqlite3.Connection, check_id: int, segment: str, use_alt: bool
) -> None:
    """写入/覆盖确认（仅在事务内使用，由调用方统一 commit）。"""
    conn.execute(
        "INSERT INTO check_confirmations (check_id, segment, use_alt, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(check_id, segment) "
        "DO UPDATE SET use_alt = excluded.use_alt, updated_at = excluded.updated_at",
        (check_id, segment, 1 if use_alt else 0, _now()),
    )


def _apply_confirmation(
    conn: sqlite3.Connection,
    check: sqlite3.Row,
    segment: str,
    use_alt: bool | None,
) -> int | None:
    """在同一事务里更新确认，并把页面沿用方案写入已算结果。

    ``use_alt=None`` 表示撤销确认。确认写入与「沿用的方案」一起提交：
    任何一步失败都会整体回滚，页面仍显示此前的确认与结果。
    """
    data = json.loads(check["precomputed"])
    confirmations = _load_confirmations(conn, check["id"])
    if use_alt is None:
        confirmations.pop(segment, None)
        conn.execute(
            "DELETE FROM check_confirmations WHERE check_id = ? AND segment = ?",
            (check["id"], segment),
        )
    else:
        confirmations[segment] = use_alt
        _save_confirmation(conn, check["id"], segment, use_alt)
    # 确认时尽量沿用此前列出的方案（未确认测段不无故跳变）；撤销时重新
    # 选「备选值最少」的可行方案，使该段回到建议取值。
    prefer = data.get("selected") if use_alt is not None else None
    mask = select_mask(data, confirmations, prefer=prefer)
    data["selected"] = mask
    conn.execute(
        "UPDATE checks SET precomputed = ? WHERE id = ?",
        (json.dumps(data), check["id"]),
    )
    conn.commit()
    return mask


def _check_context(
    conn: sqlite3.Connection, project: sqlite3.Row, check: sqlite3.Row
) -> dict:
    segments = _segments_from_json(check["segments"])
    data = json.loads(check["precomputed"])
    confirmations = _load_confirmations(conn, check["id"])
    # 本次结果沿用上次列出的方案（data["selected"]），从而「确认后页面给出的
    # 测段选择和路线」与已确认值一致，且未确认测段不会无故跳变。
    mask = select_mask(data, confirmations, prefer=data.get("selected"))

    grid_cm = json.loads(check["input_cm"])
    outlet = (check["outlet_row"], check["outlet_col"])
    source_a = (check["source_a_row"], check["source_a_col"])
    source_b = (check["source_b_row"], check["source_b_col"])

    segment_of: dict[tuple[int, int], str] = {}
    for name, cells in segments.items():
        for cell in cells:
            segment_of[cell] = name

    result = None
    problems: list[str] = []
    flow_effective = None
    if mask is not None:
        entry = data["masks"][str(mask)]
        path_a = [tuple(cell) for cell in entry["path_a"]]
        path_b = [tuple(cell) for cell in entry["path_b"]]
        flows = decode_flows(entry["flows"], data["rows"], data["cols"])
        problems = verify_paths(
            entry["flows"], data["rows"], data["cols"],
            outlet, source_a, source_b, path_a, path_b,
        )
        choices = mask_choices(data["segment_names"], mask)
        effective = effective_grid(
            grid_cm, data["segment_names"], segments, mask
        )
        flow_effective = _arrow_table_from_flows(flows)
        result = {
            "mask": mask,
            "entry": entry,
            "path_a": path_a,
            "path_b": path_b,
            "flows": flows,
            "choices": choices,
            "effective": effective,
            "cell_index_a": {cell: i + 1 for i, cell in enumerate(path_a)},
            "cell_index_b": {cell: i + 1 for i, cell in enumerate(path_b)},
        }

    return {
        "project": project,
        "check": check,
        "snapshot_id": check["snapshot_id"],
        "grid_cm": grid_cm,
        "outlet": outlet,
        "source_a": source_a,
        "source_b": source_b,
        "segments": segments,
        "segment_of": segment_of,
        "segment_names": data["segment_names"],
        "confirmations": confirmations,
        "mask": mask,
        "result": result,
        "problems": problems,
        "suggested": data.get("suggested"),
        "any_feasible": data.get("suggested") is not None,
        "flow_cm": _arrow_table(grid_cm),
        "flow_effective": flow_effective,
        "max_segments": MAX_SEGMENTS,
    }


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/")
def index():
    conn = get_db()
    projects = conn.execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM locks l WHERE l.project_id = p.id) AS lock_count,
               (SELECT status FROM snapshots s WHERE s.project_id = p.id
                ORDER BY id DESC LIMIT 1) AS last_status
        FROM projects p ORDER BY id DESC
        """
    ).fetchall()
    return render_template("index.html", projects=projects, max_dim=MAX_DIM)


@app.route("/projects", methods=["POST"])
def create_project():
    conn = get_db()
    name = (request.form.get("name") or "").strip() or "未命名底图"
    try:
        text = _read_upload(request.files.get("csv_file"))
        grid = parse_csv(text)
        error_limit = _parse_error_limit(request.form.get("error_limit", ""))
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    cur = conn.execute(
        "INSERT INTO projects (name, rows, cols, created_at) VALUES (?, ?, ?, ?)",
        (name, len(grid), len(grid[0]), _now()),
    )
    project_id = cur.lastrowid

    result = convert(grid, {}, error_limit)
    if result["feasible"]:
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "initial", request.files["csv_file"].filename,
                json.dumps(grid), error_limit, result["min_error_cm"], "ok",
                json.dumps(result["grid_dm"]), json.dumps(result["errors"]), _now(),
            ),
        )
        flash(
            f"转换成功：实际最大误差 {result['max_abs_error']} cm，"
            f"该底图最小可行误差为 {result['min_error_cm']} cm。",
            "ok",
        )
    else:
        status = "tight" if result["reason"] == "error_too_tight" else "conflict"
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "initial", request.files["csv_file"].filename,
                json.dumps(grid), error_limit,
                result.get("min_error_cm"), status, None, None, _now(),
            ),
        )
        if status == "tight":
            flash(
                f"误差限内无解。最小可行误差为 {result['min_error_cm']} cm，"
                "请放宽误差后重新转换；锁定与已生成底图均保留。",
                "error",
            )
        else:
            flash("无解：当前锁定值与流向要求无法同时满足（任何误差限都不行）。", "error")
    conn.commit()
    return redirect(url_for("view_project", project_id=project_id))


@app.route("/projects/<int:project_id>")
def view_project(project_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    return render_template("project.html", **_dashboard_context(conn, project))


@app.route("/projects/<int:project_id>/convert", methods=["POST"])
def reconvert(project_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    try:
        error_limit = _parse_error_limit(request.form.get("error_limit", ""))
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("view_project", project_id=project_id))

    # 重新转换始终基于当前工作底图（最新成功快照的厘米输入）；
    # 若从未成功过，则退回项目最初上传的那份输入。失败尝试不会改变它。
    ok = _latest_ok_snapshot(conn, project_id)
    source = ok if ok is not None else conn.execute(
        "SELECT * FROM snapshots WHERE project_id = ? AND kind = 'initial' "
        "ORDER BY id ASC LIMIT 1", (project_id,)
    ).fetchone()
    grid = json.loads(source["input_cm"])
    locks = _load_locks(conn, project_id)

    result = convert(grid, locks, error_limit)
    if result["feasible"]:
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "reconvert", None, json.dumps(grid), error_limit,
                result["min_error_cm"], "ok",
                json.dumps(result["grid_dm"]), json.dumps(result["errors"]), _now(),
            ),
        )
        flash(
            f"转换成功：实际最大误差 {result['max_abs_error']} cm，"
            f"最小可行误差 {result['min_error_cm']} cm，锁定值已沿用。",
            "ok",
        )
    else:
        status = "tight" if result["reason"] == "error_too_tight" else "conflict"
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "reconvert", None, json.dumps(grid), error_limit,
                result.get("min_error_cm"), status, None, None, _now(),
            ),
        )
        if status == "tight":
            flash(
                f"误差限内无解，已保留锁定值和上一份可下载底图。"
                f"最小可行误差为 {result['min_error_cm']} cm。",
                "error",
            )
        else:
            flash(
                "放宽误差仍无解：锁定值与流向要求无法同时满足。"
                "已保留锁定值和上一份可下载底图，请调整或删除锁定。",
                "error",
            )
    conn.commit()
    return redirect(url_for("view_project", project_id=project_id))


@app.route("/projects/<int:project_id>/locks", methods=["POST"])
def add_lock(project_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    try:
        row = int(request.form.get("row", "")) - 1  # 页面用 1 基坐标
        col = int(request.form.get("col", "")) - 1
        value = int(request.form.get("dm_value", ""))
    except (TypeError, ValueError):
        flash("行、列、分米值都必须是整数", "error")
        return redirect(url_for("view_project", project_id=project_id))
    if not (0 <= row < project["rows"] and 0 <= col < project["cols"]):
        flash(f"锁定位置超出 {project['rows']}×{project['cols']} 格网范围", "error")
        return redirect(url_for("view_project", project_id=project_id))

    conn.execute(
        "INSERT INTO locks (project_id, row_idx, col_idx, dm_value, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(project_id, row_idx, col_idx) "
        "DO UPDATE SET dm_value = excluded.dm_value",
        (project_id, row, col, value, _now()),
    )
    conn.commit()
    flash(f"已锁定第 {row + 1} 行第 {col + 1} 列 = {value} dm，请重新转换。", "ok")
    return redirect(url_for("view_project", project_id=project_id))


@app.route("/projects/<int:project_id>/locks/<int:row>/<int:col>/delete", methods=["POST"])
def delete_lock(project_id: int, row: int, col: int):
    conn = get_db()
    conn.execute(
        "DELETE FROM locks WHERE project_id = ? AND row_idx = ? AND col_idx = ?",
        (project_id, row, col),
    )
    conn.commit()
    flash("已删除该锁定，请重新转换。", "ok")
    return redirect(url_for("view_project", project_id=project_id))


@app.route("/projects/<int:project_id>/correction", methods=["POST"])
def upload_correction(project_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    try:
        text = _read_upload(request.files.get("csv_file"))
        new_grid = parse_csv(text)
        error_limit = _parse_error_limit(request.form.get("error_limit", ""))
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("view_project", project_id=project_id))

    if len(new_grid) != project["rows"] or len(new_grid[0]) != project["cols"]:
        flash(
            f"测绘更正尺寸 {len(new_grid)}×{len(new_grid[0])} 与原底图 "
            f"{project['rows']}×{project['cols']} 不一致，已拒绝。",
            "error",
        )
        return redirect(url_for("view_project", project_id=project_id))

    locks = _load_locks(conn, project_id)
    result = convert(new_grid, locks, error_limit)
    # 无论成败都记录尝试（grid_dm 为 NULL）；可下载底图始终取最新成功快照。
    if result["feasible"]:
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "correction", request.files["csv_file"].filename,
                json.dumps(new_grid), error_limit, result["min_error_cm"], "ok",
                json.dumps(result["grid_dm"]), json.dumps(result["errors"]), _now(),
            ),
        )
        flash(
            f"测绘更正转换成功：沿用 {len(locks)} 个锁定值，"
            f"实际最大误差 {result['max_abs_error']} cm。",
            "ok",
        )
    else:
        conn.execute(
            "INSERT INTO snapshots (project_id, kind, source_name, input_cm, error_limit,"
            " min_error, status, grid_dm, errors, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, "correction", request.files["csv_file"].filename,
                json.dumps(new_grid), error_limit,
                result.get("min_error_cm"),
                "tight" if result["reason"] == "error_too_tight" else "conflict",
                None, None, _now(),
            ),
        )
        if result["reason"] == "error_too_tight":
            flash(
                f"更正底图在误差限内无解，已保留锁定和上一份可下载底图。"
                f"最小可行误差为 {result['min_error_cm']} cm。",
                "error",
            )
        else:
            flash(
                "更正底图与锁定值在任何误差限下都无法同时满足流向一致，"
                "已保留锁定和上一份可下载底图。",
                "error",
            )
    conn.commit()
    return redirect(url_for("view_project", project_id=project_id))


@app.route("/projects/<int:project_id>/download")
def download_dm(project_id: int):
    conn = get_db()
    snap = _latest_ok_snapshot(conn, project_id)
    if snap is None:
        flash("当前没有可下载的分米底图", "error")
        return redirect(url_for("view_project", project_id=project_id))
    grid_dm = json.loads(snap["grid_dm"])
    csv_text = to_csv(grid_dm)
    filename = f"basemap_{project_id}_snapshot{snap['id']}_dm.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
def delete_project(project_id: int):
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    flash("底图任务已删除", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# 双路来水核查
# --------------------------------------------------------------------------- #
def _check_or_404(conn: sqlite3.Connection, project_id: int, check_id: int):
    return conn.execute(
        "SELECT * FROM checks WHERE id = ? AND project_id = ?",
        (check_id, project_id),
    ).fetchone()


def _parse_cell(form, prefix: str) -> tuple[int, int]:
    try:
        row = int(form.get(f"{prefix}_row", "")) - 1
        col = int(form.get(f"{prefix}_col", "")) - 1
    except (TypeError, ValueError) as exc:
        raise ValidationError("来水格与沟口的行、列都必须是整数") from exc
    return row, col


@app.route("/projects/<int:project_id>/checks", methods=["POST"])
def create_check(project_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))

    def fail(message: str):
        flash(message, "error")
        return redirect(url_for("view_project", project_id=project_id))

    try:
        snapshot_id = int(request.form.get("snapshot_id", ""))
    except ValueError:
        return fail("请选择一份转换成功的快照")
    snapshot = conn.execute(
        "SELECT * FROM snapshots WHERE id = ? AND project_id = ? AND status = 'ok'",
        (snapshot_id, project_id),
    ).fetchone()
    if snapshot is None:
        return fail("所选快照不存在或不是转换成功的快照，请重新选择")

    try:
        outlet = _parse_cell(request.form, "outlet")
        source_a = _parse_cell(request.form, "source_a")
        source_b = _parse_cell(request.form, "source_b")
    except ValidationError as exc:
        return fail(str(exc))

    for label, (r, c) in (("沟口", outlet), ("来水甲", source_a), ("来水乙", source_b)):
        if not (0 <= r < project["rows"] and 0 <= c < project["cols"]):
            return fail(
                f"{label} ({r + 1},{c + 1}) 超出 {project['rows']}×{project['cols']} 格网范围"
            )
    if len({outlet, source_a, source_b}) != 3:
        return fail("来水甲、来水乙与沟口必须是三个不同的格子，位置不能重合")

    try:
        text = _read_upload(request.files.get("csv_file"))
        segments = parse_alternatives(text)
    except ValidationError as exc:
        return fail(str(exc))

    rows, cols = project["rows"], project["cols"]
    for name, cells in segments.items():
        for (r, c) in cells:
            if not (0 <= r < rows and 0 <= c < cols):
                return fail(
                    f"测段「{name}」中的 ({r + 1},{c + 1}) 超出 {rows}×{cols} 格网范围"
                )

    # 冻结所用快照的厘米底图；后续测绘更正不影响这次核查。
    # CSV 未列出的格子（含关键格）始终使用快照厘米值，不受测段取值影响。
    grid_cm = json.loads(snapshot["input_cm"])
    data = precompute(grid_cm, outlet, source_a, source_b, segments)

    cur = conn.execute(
        "INSERT INTO checks (project_id, snapshot_id, kind, outlet_row, outlet_col,"
        " source_a_row, source_a_col, source_b_row, source_b_col, input_cm, segments,"
        " precomputed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id, snapshot_id, "dual",
            outlet[0], outlet[1], source_a[0], source_a[1], source_b[0], source_b[1],
            json.dumps(grid_cm), _segments_to_json(segments),
            json.dumps(data), _now(),
        ),
    )
    check_id = cur.lastrowid
    conn.commit()
    if data["suggested"] is None:
        flash(
            "已建核查，但找不到双路：没有任何一套测段取值能让两条来水各走一条支路"
            "在沟口相交。",
            "error",
        )
    else:
        flash(f"核查已建立（#{check_id}），并给出一套成立的测段选择与两条逐格路线。", "ok")
    return redirect(url_for("view_check", project_id=project_id, check_id=check_id))


@app.route("/projects/<int:project_id>/checks/<int:check_id>")
def view_check(project_id: int, check_id: int):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    check = _check_or_404(conn, project_id, check_id)
    if check is None:
        flash("核查不存在", "error")
        return redirect(url_for("view_project", project_id=project_id))
    snapshot = conn.execute(
        "SELECT id, kind, source_name, created_at, input_cm FROM snapshots WHERE id = ?",
        (check["snapshot_id"],),
    ).fetchone()
    return render_template(
        "check.html", snapshot=snapshot, **_check_context(conn, project, check)
    )


@app.route("/projects/<int:project_id>/checks/<int:check_id>/confirm", methods=["POST"])
def confirm_segment(project_id: int, check_id: int):
    conn = get_db()
    check = _check_or_404(conn, project_id, check_id)
    if check is None:
        flash("核查不存在", "error")
        return redirect(url_for("view_project", project_id=project_id))
    segment = request.form.get("segment", "")
    use_alt = request.form.get("use_alt") == "1"
    segments = _segments_from_json(check["segments"])
    if segment not in segments:
        flash("该测段不属于此核查", "error")
        return redirect(url_for("view_check", project_id=project_id, check_id=check_id))
    try:
        result_mask = _apply_confirmation(conn, check, segment, use_alt)
    except sqlite3.Error as exc:
        conn.rollback()
        # 保存失败：确认与沿用方案都未提交，页面仍是此前的确认与结果。
        app.logger.exception("保存确认失败: %s", exc)
        flash("确认保存失败，页面仍显示此前的确认与核查结果。请稍后重试。", "error")
        return redirect(url_for("view_check", project_id=project_id, check_id=check_id))
    label = "备选值" if use_alt else "快照值"
    if result_mask is None:
        flash(
            f"已确认测段「{segment}」使用{label}；但在这套确认下已找不到双路，"
            "请撤销或调整其他测段的确认。",
            "error",
        )
        return redirect(url_for("view_check", project_id=project_id, check_id=check_id))
    flash(
        f"已确认测段「{segment}」使用{label}；结果与路线已按已算流向更新。",
        "ok",
    )
    return redirect(url_for("view_check", project_id=project_id, check_id=check_id))


@app.route("/projects/<int:project_id>/checks/<int:check_id>/revoke", methods=["POST"])
def revoke_segment(project_id: int, check_id: int):
    conn = get_db()
    check = _check_or_404(conn, project_id, check_id)
    if check is None:
        flash("核查不存在", "error")
        return redirect(url_for("view_project", project_id=project_id))
    segment = request.form.get("segment", "")
    segments = _segments_from_json(check["segments"])
    if segment not in segments:
        flash("该测段不属于此核查", "error")
        return redirect(url_for("view_check", project_id=project_id, check_id=check_id))
    try:
        _apply_confirmation(conn, check, segment, None)
    except sqlite3.Error as exc:
        conn.rollback()
        app.logger.exception("撤销确认失败: %s", exc)
        flash("撤销确认失败，页面仍显示此前的确认与核查结果。请稍后重试。", "error")
        return redirect(url_for("view_check", project_id=project_id, check_id=check_id))
    flash(f"已撤销测段「{segment}」的确认，该段重新按建议取值；结果与路线已按已算流向更新。", "ok")
    return redirect(url_for("view_check", project_id=project_id, check_id=check_id))


@app.route("/projects/<int:project_id>/checks/template")
def check_template(project_id: int):
    """下载一份可直接改用的备选高程 CSV 模板（按格网尺寸给出三个关键格骨架）。"""
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        flash("底图不存在", "error")
        return redirect(url_for("index"))
    rows, cols = project["rows"], project["cols"]
    ok = _latest_ok_snapshot(conn, project_id)
    grid: list[list[int]] = json.loads(ok["input_cm"]) if ok is not None else []
    source_a = (0, 0)
    source_b = (0, cols - 1)
    outlet = (rows - 1, cols - 1)
    template: dict[str, dict[tuple[int, int], int]] = {
        "来水甲测段": {source_a: grid[source_a[0]][source_a[1]] if grid else 0},
        "来水乙测段": {source_b: grid[source_b[0]][source_b[1]] if grid else 0},
        "沟口": {outlet: grid[outlet[0]][outlet[1]] if grid else 0},
    }
    csv_text = to_alternative_csv(template)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=check_{project_id}_alternatives_template.csv"
        },
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
