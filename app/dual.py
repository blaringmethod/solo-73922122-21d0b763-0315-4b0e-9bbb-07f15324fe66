"""双路来水核查：两条来水各走一条支路、只在沟口相交。

约定
====
* 核查绑定一份**转换成功（status='ok'）的快照**。流向按该快照自己的厘米
  底图计算（四邻中最低且严格低于本格的全部邻格），与转换时保证的流向一致；
  之后上传的测绘更正只新增快照，不会改变已建核查所用的快照。
* 备选高程 CSV 每行 ``测段,行号,列号,备选厘米值``（行/列从 1 开始），
  最多 8 个测段，每格最多出现一次。
* 每个测段二选一：整段统一用快照厘米值，或整段统一用备选厘米值。
  两条路线必须在**同一套**测段取值（同一 mask）下成立。
* 双路：两个来水格各自沿四邻流向走到沟口，两条逐格路线仅在沟口相交。
* 建立核查时一次算好全部 ``2**测段数`` 套取值下的流向与双路结果并持久化；
  之后的确认/撤销只读取这套已算结果，不重新转换、不重算流向。
"""

from __future__ import annotations

from collections import deque

from .solver import NEIGHBORS, ValidationError, flow_sets

MAX_SEGMENTS = 8

# 与 solver.NEIGHBORS 同序：上/下/左/右 -> 位 1/2/4/8。
_DIR_BITS = {step: 1 << i for i, step in enumerate(NEIGHBORS)}

Cell = tuple[int, int]

_HEADER_TOKENS = {"测段", "行号", "列号", "备选厘米值", "segment", "row", "col", "value"}


def _looks_like_header(tokens: list[str]) -> bool:
    return all(tok.lower() in _HEADER_TOKENS for tok in tokens)


# --------------------------------------------------------------------------- #
# 备选高程 CSV
# --------------------------------------------------------------------------- #
def parse_alternatives(text: str) -> dict[str, dict[Cell, int]]:
    """解析备选高程 CSV，返回 ``{测段名: {(row0, col0): 备选厘米值}}``。

    可带一行表头（``测段,行号,列号,备选厘米值``，首行非整数时按表头跳过）。
    校验：4 列；行/列/值为整数；行/列从 1 开始；最多 8 段；
    每格只填写一次（跨测段重复也拒绝）。
    """
    segments: dict[str, dict[Cell, int]] = {}
    seen: dict[Cell, str] = {}
    saw_data = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        tokens = [tok.strip() for tok in line.split(",")]
        if len(tokens) != 4:
            raise ValidationError(
                f"备选高程第 {lineno} 行应为 4 列：测段,行号,列号,备选厘米值"
            )
        if not saw_data and _looks_like_header(tokens):
            continue
        saw_data = True
        name, row_s, col_s, value_s = tokens
        if not name:
            raise ValidationError(f"备选高程第 {lineno} 行缺少测段名")
        try:
            row, col, value = int(row_s), int(col_s), int(value_s)
        except ValueError as exc:
            raise ValidationError(
                f"备选高程第 {lineno} 行的行号/列号/厘米值必须为整数"
            ) from exc
        if row < 1 or col < 1:
            raise ValidationError(f"备选高程第 {lineno} 行号与列号必须从 1 开始")
        cell = (row - 1, col - 1)
        if cell in seen:
            raise ValidationError(
                f"第 {row} 行第 {col} 列在测段「{seen[cell]}」与「{name}」中重复填写，"
                "每格只能填写一次"
            )
        seen[cell] = name
        segments.setdefault(name, {})[cell] = value
        if len(segments) > MAX_SEGMENTS:
            raise ValidationError(f"备选高程最多支持 {MAX_SEGMENTS} 个测段")
    if not segments:
        raise ValidationError("备选高程 CSV 为空，至少需要一个测段的格子")
    return segments


def to_alternative_csv(segments: dict[str, dict[Cell, int]]) -> str:
    lines = ["测段,行号,列号,备选厘米值"]
    for name in sorted(segments):
        for (r, c), v in sorted(segments[name].items()):
            lines.append(f"{name},{r + 1},{c + 1},{v}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 流向压缩（逐格 4 位：上/下/左/右）
# --------------------------------------------------------------------------- #
def encode_flows(flows: list[list[list[Cell]]]) -> str:
    out: list[str] = []
    for r, row in enumerate(flows):
        for c, targets in enumerate(row):
            bits = 0
            for nr, nc in targets:
                bits |= _DIR_BITS[(nr - r, nc - c)]
            out.append(format(bits, "x"))
    return "".join(out)


def decode_flows(encoded: str, rows: int, cols: int) -> list[list[list[Cell]]]:
    flows: list[list[list[Cell]]] = [[[] for _ in range(cols)] for _ in range(rows)]
    if len(encoded) != rows * cols:
        raise ValueError("流向数据长度与格网尺寸不符")
    for r in range(rows):
        for c in range(cols):
            bits = int(encoded[r * cols + c], 16)
            targets = []
            for i, (dr, dc) in enumerate(NEIGHBORS):
                if bits & (1 << i):
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        raise ValueError("流向数据包含越界邻格")
                    targets.append((nr, nc))
            flows[r][c] = targets
    return flows


# --------------------------------------------------------------------------- #
# 两条只在沟口相交的路线
# --------------------------------------------------------------------------- #
def _max_flow_two(
    flows: list[list[list[Cell]]],
    source_a: Cell,
    source_b: Cell,
    outlet: Cell,
) -> dict[Cell, Cell] | None:
    """节点拆点 + Edmonds–Karp（只推 2 个单位）判定并返回实际使用的流边。

    非沟口格通过容量 1，沟口容量 2；两个来水各从超级源取 1 单位。
    可达 2 单位即说明存在两条只在沟口相交的路线。
    """
    rows, cols = len(flows), len(flows[0])
    n = rows * cols

    def inn(cell: Cell) -> int:
        return 2 * (cell[0] * cols + cell[1])

    def out(cell: Cell) -> int:
        return inn(cell) + 1

    # 邻接表：每条边 [to, rev_index, residual, original]
    graph: list[list[list[int]]] = [[] for _ in range(2 * n + 1)]

    def add(u: int, v: int, cap: int) -> None:
        graph[u].append([v, len(graph[v]), cap, cap])
        graph[v].append([u, len(graph[u]) - 1, 0, 0])

    super_src = 2 * n
    add(super_src, inn(source_a), 1)
    add(super_src, inn(source_b), 1)

    tracked: list[tuple[int, int]] = []  # (u, edge_index) 的格间流边
    for r in range(rows):
        for c in range(cols):
            cell = (r, c)
            add(inn(cell), out(cell), 2 if cell == outlet else 1)
            for target in flows[r][c]:
                tracked.append((out(cell), len(graph[out(cell)])))
                add(out(cell), inn(target), 1)

    sink = out(outlet)
    total = 0
    while total < 2:
        parent: list[tuple[int, int] | None] = [None] * len(graph)
        parent[super_src] = (-1, -1)
        q = deque([super_src])
        while q and parent[sink] is None:
            u = q.popleft()
            for idx, edge in enumerate(graph[u]):
                v, _rev, residual, _orig = edge
                if residual > 0 and parent[v] is None:
                    parent[v] = (u, idx)
                    q.append(v)
        if parent[sink] is None:
            break
        # 沿增广路径推 1 单位
        v = sink
        while v != super_src:
            u, idx = parent[v]  # type: ignore[misc]
            edge = graph[u][idx]
            edge[2] -= 1
            graph[v][edge[1]][2] += 1
            v = u
        total += 1

    if total < 2:
        return None

    used: dict[Cell, Cell] = {}
    for u, idx in tracked:
        edge = graph[u][idx]
        if edge[3] == 1 and edge[2] == 0:  # 原始容量 1 且已用满
            src_idx, dst_idx = u // 2, edge[0] // 2
            used[(src_idx // cols, src_idx % cols)] = (dst_idx // cols, dst_idx % cols)
    return used


def two_disjoint_paths(
    flows: list[list[list[Cell]]], source_a: Cell, source_b: Cell, outlet: Cell
) -> tuple[list[Cell], list[Cell]] | None:
    """在给定逐格流向中找两条只在 outlet 相交的路线；找不到返回 None。"""
    used = _max_flow_two(flows, source_a, source_b, outlet)
    if used is None:
        return None
    paths: list[list[Cell]] = []
    for start in (source_a, source_b):
        path = [start]
        cur = start
        for _ in range(len(flows) * len(flows[0]) + 1):
            if cur == outlet:
                break
            nxt = used.get(cur)
            if nxt is None:
                return None
            path.append(nxt)
            cur = nxt
        if cur != outlet:
            return None
        paths.append(path)
    path_a, path_b = paths
    if set(path_a) & set(path_b) != {outlet}:
        return None
    return path_a, path_b


# --------------------------------------------------------------------------- #
# 测段取值组合
# --------------------------------------------------------------------------- #
def effective_grid(
    grid_cm: list[list[int]],
    segment_names: list[str],
    segments: dict[str, dict[Cell, int]],
    mask: int,
) -> list[list[int]]:
    """按 mask（第 i 位对应 segment_names[i]，1=备选值）生成厘米格网。"""
    grid = [row[:] for row in grid_cm]
    for i, name in enumerate(segment_names):
        if mask & (1 << i):
            for (r, c), value in segments[name].items():
                grid[r][c] = value
    return grid


def mask_choices(segment_names: list[str], mask: int) -> dict[str, bool]:
    return {name: bool(mask & (1 << i)) for i, name in enumerate(segment_names)}


def _mask_pref(mask: int) -> tuple[int, int]:
    return (mask.bit_count(), mask)


def precompute(
    grid_cm: list[list[int]],
    outlet: Cell,
    source_a: Cell,
    source_b: Cell,
    segments: dict[str, dict[Cell, int]],
) -> dict:
    """一次算好全部 2**N 套取值下的流向与双路结果（可 JSON 持久化）。

    返回 ``{"segment_names": [...], "masks": {str(mask): {...}}, "suggested": int|None,
    "selected": int|None}``。``suggested`` 记录是否存在任何可行组合；``selected``
    是页面当前列出的方案，确认/撤销时在事务内更新以保持稳定。
    只保存成立（ok）组合的流向与路线；不成立组合只留 ``ok=False``。
    """
    rows, cols = len(grid_cm), len(grid_cm[0])
    segment_names = sorted(segments)
    masks: dict[str, dict] = {}
    for mask in range(1 << len(segment_names)):
        grid = effective_grid(grid_cm, segment_names, segments, mask)
        flows = flow_sets(grid)
        pair = two_disjoint_paths(flows, source_a, source_b, outlet)
        if pair is None:
            masks[str(mask)] = {"ok": False}
            continue
        path_a, path_b = pair
        if set(path_a) & set(path_b) != {outlet}:
            masks[str(mask)] = {"ok": False}
            continue
        masks[str(mask)] = {
            "ok": True,
            "flows": encode_flows(flows),
            "path_a": [list(cell) for cell in path_a],
            "path_b": [list(cell) for cell in path_b],
        }
    feasible = [int(k) for k, v in masks.items() if v["ok"]]
    suggested = min(feasible, key=_mask_pref) if feasible else None
    return {
        "segment_names": segment_names,
        "masks": masks,
        "suggested": suggested,
        "selected": suggested,
        "rows": rows,
        "cols": cols,
    }


def select_mask(
    data: dict, confirmed: dict[str, bool], prefer: int | None = None
) -> int | None:
    """在已确认取值约束下，从已算结果中挑一套成立的方案。

    未被确认的测段可自由选择；若 ``prefer``（此前列出的方案）仍满足全部确认，
    优先沿用它，避免确认一段时路线无谓跳动；否则取「备选值最少」的方案，
    再按 mask 升序稳定选择。
    """
    segment_names: list[str] = data["segment_names"]
    allowed = [
        int(key)
        for key, result in data["masks"].items()
        if result.get("ok")
        and all(
            bool(int(key) & (1 << segment_names.index(name))) == value
            for name, value in confirmed.items()
            if name in segment_names
        )
    ]
    if prefer is not None and prefer in allowed:
        return prefer
    return min(allowed, key=_mask_pref) if allowed else None


def verify_paths(
    encoded_flows: str,
    rows: int,
    cols: int,
    outlet: Cell,
    source_a: Cell,
    source_b: Cell,
    path_a: list[Cell],
    path_b: list[Cell],
) -> list[str]:
    """用已算好的压缩流向独立复核两条路线，返回问题列表（空表示通过）。"""
    problems: list[str] = []
    flows = decode_flows(encoded_flows, rows, cols)
    for label, path, start in (("来水甲", path_a, source_a), ("来水乙", path_b, source_b)):
        if not path or path[0] != start:
            problems.append(f"{label}路线起点不符")
            continue
        if path[-1] != outlet:
            problems.append(f"{label}路线终点不是沟口")
        for src, dst in zip(path, path[1:]):
            if dst not in flows[src[0]][src[1]]:
                problems.append(f"{label} {src}->{dst} 不是已算流向")
    if set(path_a) & set(path_b) != {outlet}:
        problems.append("两条路线在沟口以外相交")
    return problems
