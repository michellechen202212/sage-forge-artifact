"""
SAGE evidence adapter (prototype).

Extracts *mechanical evidence* from a data-transformation artifact. It reports
structural facts only; it never asserts business semantics, and it REFUSEs
explicitly whenever it cannot faithfully analyse a construct a requirement
depends on.

Five evidence questions, one per semantic dimension of the architecture. The
names below describe what THIS implementation computes, which is narrower than
the dimensions themselves:

  E1 grain_construct   - which construct fixes the output's row grain
                         (GROUP BY / DISTINCT / window-dedup / base x joins).
  E2 join_structure    - joins, their equality keys, and whether two or more
                         relations join to the same parent key.
  E3 fanout_measure    - the structural multi-child fan-out shape, and which
                         columns of such a parent are projected. NOT a general
                         duplication analysis: without a catalog the adapter
                         cannot know whether the children are non-unique, so
                         cardinality remains the contract's job.
  E4 time_columns      - columns whose NAME matches a time-like suffix, and
                         whether such columns appear in filters or windows.
                         A name heuristic: no type resolution, no temporal
                         semantics.
  E5 relation_lineage  - source relations and output column names. NOT
                         column-level lineage, transformation paths, or
                         aggregation lineage.

Each question returns ANSWERED (with evidence) or REFUSED (with a reason).
"""
from __future__ import annotations
import re, json, dataclasses
from typing import Any
import sqlglot
from sqlglot import exp

# One pinned dialect for the frozen artifact. No fallbacks: a silent reparse
# under a different grammar would make "the pinned adapter" untrue, and a
# dialect mismatch is exactly the kind of incapability REFUSE exists to report.
PRIMARY_DIALECT = "snowflake"

# ---------------------------------------------------------------- templating

CONFIG_BLOCK = re.compile(r"\{\{-?\s*config\s*\(.*?\)\s*-?\}\}", re.S | re.I)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
STMT_TAG = re.compile(r"\{%.*?%\}", re.S)
EXPR_TAG = re.compile(r"\{\{.*?\}\}", re.S)


class _Unresolvable(Exception):
    """Raised when expansion depends on a value the adapter must not invent."""


def _mk_env():
    import jinja2

    def ref(*a):
        return a[-1].replace(".", "__")

    def source(a, b):
        return f"{a}__{b}"

    def config(*a, **k):
        return ""

    def var(name, *a, **k):
        # A defaulted var is still a project variable: dbt_project.yml or
        # --vars routinely overrides it, so taking the default would be a
        # guess delivered as evidence. Refuse either way.
        raise _Unresolvable(f"var:{name}")

    class _BlockUndefined(jinja2.Undefined):
        """Any name the adapter holds no definition for is unresolvable."""
        def _bail(self, *a, **k):
            raise _Unresolvable(f"macro:{self._undefined_name}")
        __call__ = _bail
        __str__ = _bail
        __iter__ = _bail
        __len__ = _bail
        __bool__ = _bail
        __eq__ = _bail
        __getitem__ = _bail
        _fail_with_undefined_error = _bail

        def __getattr__(self, k):
            if k.startswith("__"):
                raise AttributeError(k)
            return _BlockUndefined(name=f"{self._undefined_name}.{k}")

    env = jinja2.Environment(undefined=_BlockUndefined)
    env.globals.update(ref=ref, source=source, config=config, var=var,
                       this="this_model")
    return env


def resolve_templating(raw: str) -> tuple[str, str | None, str | None]:
    """Documented pre-pass: strip config blocks, then render Jinja with stubs
    for dbt's relation functions.

    Control flow (``{% for %}``, ``{% set %}``) is executed, because its
    expansion is determined by the model text itself. Anything whose expansion
    depends on project variables, target configuration, or a macro body the
    adapter does not hold is REFUSEd -- guessing it would be exactly the
    'adapter infers business policy' failure the architecture forbids.
    """
    import jinja2
    s = JINJA_COMMENT.sub(" ", raw)
    s = CONFIG_BLOCK.sub(" ", s)

    env = _mk_env()

    try:
        rendered = env.from_string(s).render()
    except _Unresolvable as e:
        kind, _, name = str(e).partition(":")
        if kind == "macro" and name.split(".")[0] in ("target", "this", "env_var", "builtins"):
            detail = "target_or_env_config"
        else:
            detail = {"macro": "undefined_macro",
                      "var": "project_variable"}.get(kind, "unresolved")
        return s, "unresolved_templating", detail
    except jinja2.TemplateError as e:
        return s, "unresolved_templating", f"template_error:{type(e).__name__}"
    except Exception as e:
        return s, "unresolved_templating", f"render_error:{type(e).__name__}"

    if STMT_TAG.search(rendered) or EXPR_TAG.search(rendered):   # defensive
        return rendered, "unresolved_templating", "residual_jinja"
    if not re.search(r"\bselect\b", rendered, re.I):
        return rendered, "unresolved_templating", "no_sql_after_expansion"
    return rendered.strip(), None, None


# ------------------------------------------------------------------ parsing

def parse(sql: str) -> tuple[Any, str | None, str | None]:
    """Parse with the pinned dialect. Failure is REFUSE, not a reparse."""
    try:
        return sqlglot.parse_one(sql, dialect=PRIMARY_DIALECT), PRIMARY_DIALECT, None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:120]}"


# ------------------------------------------------------- structure helpers

def _final_select(tree) -> exp.Select | None:
    """The SELECT that produces the model's output rows."""
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, (exp.Union, exp.Except, exp.Intersect)):
        return None  # set operation: grain is the union of arms
    sel = tree.find(exp.Select)
    return sel


def _eq_keys(cond) -> list[tuple[str, str, str, str]]:
    """Equality predicates as (lhs_rel, lhs_col, rhs_rel, rhs_col)."""
    out = []
    if cond is None:
        return out
    for eq in cond.find_all(exp.EQ):
        l, r = eq.left, eq.right
        if isinstance(l, exp.Column) and isinstance(r, exp.Column):
            out.append((l.table or "", l.name, r.table or "", r.name))
    return out



def _arg(node, *names):
    """Read an AST arg under either the modern or legacy spelling."""
    for n in names:
        v = node.args.get(n)
        if v is not None:
            return v
    return None


def _rel_of(node):
    """(qualified_name, alias) for a FROM/JOIN operand, or None."""
    if isinstance(node, exp.Table):
        parts = [p.name for p in (node.args.get("catalog"), node.args.get("db")) if p]
        qualified = ".".join(parts + [node.name])
        return qualified, node.alias_or_name
    if isinstance(node, exp.Subquery):
        return None, node.alias_or_name or "<subquery>"
    return None


def _source_nodes(sel):
    """The operand nodes of THIS select's FROM and JOINs."""
    out = []
    f = _arg(sel, "from_", "from")
    if f is not None:
        out.append(f.this)
    for j in _arg(sel, "joins") or []:
        out.append(j.this)
    return out


def _scope_sources(sel):
    """(qualified_name, alias) pairs in THIS select's FROM and JOINs only."""
    out = []
    f = _arg(sel, "from_", "from")
    if f is not None:
        r = _rel_of(f.this)
        if r:
            out.append(r)
    for j in _arg(sel, "joins") or []:
        r = _rel_of(j.this)
        if r:
            out.append(r)
    return out


def _join_keys(cond):
    """Keys from an ON condition, accepting ONLY a conjunction of qualified
    column-to-column equalities.

    Returns (keys, status) with status in {ok, unqualified, non_conjunctive}.
    Harvesting EQ nodes anywhere in the tree would read
    ``a.id = b.id AND a.ts <= b.ts`` as a plain equality join and silently drop
    the range predicate, and would treat equalities under OR as conjunctive
    keys. Both would be guesses presented as evidence.
    """
    keys: list = []
    status = {"bad": None}

    def walk(n) -> bool:
        if isinstance(n, exp.Paren):
            return walk(n.this)
        if isinstance(n, exp.And):
            return walk(n.left) and walk(n.right)
        if isinstance(n, exp.EQ):
            l, r = n.left, n.right
            if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                if not l.table or not r.table:
                    status["bad"] = "unqualified"
                    return False
                keys.append(((l.table, l.name), (r.table, r.name)))
                return True
        status["bad"] = status["bad"] or "non_conjunctive"
        return False

    ok = walk(cond)
    return (keys, "ok") if ok else ([], status["bad"] or "non_conjunctive")


def _scope_joins(sel):
    """Joins of THIS select only, with equality keys as ((relA,colA),(relB,colB)).

    Scope matters: `tree.find_all(exp.Join)` merges joins from sibling CTEs,
    which manufactures fan-out shapes that exist in no single query scope.
    """
    base = None
    f = _arg(sel, "from_", "from")
    if f is not None:
        r = _rel_of(f.this)
        base = r[1] if r else None

    joins, using_cols = [], set()
    for j in _arg(sel, "joins") or []:
        r = _rel_of(j.this)
        alias = r[1] if r else None
        on, using = j.args.get("on"), j.args.get("using")
        kind = ((j.side or "") + " " + (j.kind or "")).strip() or "INNER"
        keys, unqualified, non_equality, no_condition, cross = [], False, False, False, False

        if using:
            # USING(c) equates c on the joined relation with c on the relation
            # it is joined to. With several prior relations in scope, which one
            # carries c is ambiguous, and attributing it to the FROM relation
            # would invent a column. Refuse instead.
            first_join = not joins
            for u in (using if isinstance(using, list) else [using]):
                col = u.name if hasattr(u, "name") else str(u)
                # The left side of a USING is the accumulated join result. On
                # the first join that is exactly the FROM relation; on a later
                # join it is only safe when this same column was already merged
                # by an earlier USING, so SQL guarantees it equals the FROM
                # relation's column. Otherwise the column may live on some
                # intermediate relation and naming the FROM relation would
                # invent it, so refuse.
                if alias and base and (first_join or col in using_cols):
                    keys.append(((base, col), (alias, col)))
                    using_cols.add(col)
                else:
                    unqualified = True
        elif on is not None:
            keys, status = _join_keys(on)
            unqualified = status == "unqualified"
            non_equality = status == "non_conjunctive"
        elif (j.kind or "").upper() == "CROSS" or "CROSS" in kind.upper():
            # A cross join multiplies unconditionally. It has no key to report,
            # so reporting "no fan-out" would be affirmative evidence of
            # something the adapter has not established.
            cross = True
        elif not j.args.get("lateral"):
            no_condition = True

        joins.append({"relation": alias, "type": kind, "keys": keys,
                      "unqualified_key": unqualified, "non_equality": non_equality,
                      "no_condition": no_condition, "cross": cross,
                      "keys_text": [f"{a}.{b}={c}.{d}" for (a, b), (c, d) in keys]})
    return joins


def _fanout(joins):
    """Relations sharing one join key: >=2 partners on a key is the multi-child shape.

    Symmetric on purpose. Keying off 'the side that is not the joined relation'
    misses the shape whenever the parent is itself introduced by a JOIN rather
    than by FROM.
    """
    partners: dict[tuple[str, str], set] = {}
    for j in joins:
        for (a, b), (c, d) in j["keys"]:
            partners.setdefault((a, b), set()).add(c)
            partners.setdefault((c, d), set()).add(a)
    return {f"{r}.{c}": sorted(v) for (r, c), v in partners.items() if len(v) >= 2}


def _cte_map(tree):
    return {c.alias_or_name: c.this for c in tree.find_all(exp.CTE)}


def _select_of(node):
    """The SELECT a node resolves to, or None when it is not a single SELECT.

    Returning `node.find(exp.Select)` for a set operation yields the FIRST ARM,
    so a union one level inside a CTE would be reported as that arm's grain.
    Refusing is the only honest answer: the arms are not reconciled.
    """
    if isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
        return None
    if isinstance(node, exp.Select):
        return node
    if isinstance(node, exp.Subquery):
        return _select_of(node.this)
    inner = node.this if hasattr(node, "this") else None
    if isinstance(inner, (exp.Union, exp.Except, exp.Intersect)):
        return None
    return node.find(exp.Select) if not isinstance(node, exp.CTE) else _select_of(node.this)


def _grain_scope(tree, cte_map):
    """The scope that fixes the output's row grain, following pass-through CTEs.

    `select * from final` does not fix grain; the CTE it reads does. Searching
    the whole tree instead would attribute a GROUP BY from any sibling CTE to
    the output, which is a confident wrong answer rather than a refusal.
    """
    sel = _final_select(tree)
    if sel is None:
        return None, "set_operation", "set_operation_arms_not_reconciled"
    seen = set()
    while True:
        if _arg(sel, "group"):
            return sel, "group_by", None
        if sel.args.get("distinct"):
            return sel, "distinct", None
        if _arg(sel, "qualify") or any(p.find(exp.Window) for p in sel.expressions):
            return sel, "window_dedup_or_base", None
        if _arg(sel, "joins"):
            return sel, "base_relation_times_join_multiplicity", None
        nodes = _source_nodes(sel)
        if len(nodes) == 0:
            return sel, "unresolved", "no_source_relation"
        if len(nodes) > 1:
            # more than one source without a JOIN node: a comma join, whose
            # multiplicity we have not analysed
            return sel, "unresolved", "comma_join_not_analysed"
        n = nodes[0]
        if isinstance(n, exp.Subquery):
            nxt = _select_of(n)
            if nxt is None:
                return sel, "unresolved", "set_operation_arms_not_reconciled"
            if id(n) in seen:
                return sel, "unresolved", "cyclic_source"
            seen.add(id(n))
            sel = nxt
            continue
        r = _rel_of(n)
        if r is None:
            # Default is REFUSE, not "base_relation". Every construct nobody
            # has thought of yet must land here rather than be cleared.
            return sel, "unresolved", "source_construct_not_analysed"
        nm = r[0]
        if nm in cte_map:
            if nm in seen:
                return sel, "unresolved", "cyclic_source"
            nxt = _select_of(cte_map[nm])
            if nxt is None:
                return sel, "unresolved", "set_operation_arms_not_reconciled"
            seen.add(nm)
            sel = nxt
            continue
        return sel, "base_relation", None


def resolve_output_columns(tree, cte_map):
    """Expand SELECT * over CTEs defined in the same query.

    A star over an *external* relation stays REFUSEd: without a catalog the
    adapter does not know those columns, and inventing them is not permitted.
    """
    def cols_of(node, seen: frozenset):
        sel = _select_of(node)
        if sel is None:
            return [], "non_select_cte"
        acc = []
        for pexp in sel.expressions:
            star_src = None
            if isinstance(pexp, exp.Star):
                nodes = _source_nodes(sel)
                if not nodes:
                    return [], "star_without_source"
                for n in nodes:
                    if isinstance(n, exp.Subquery):
                        sub, err = cols_of(n, seen)
                        if err:
                            return [], err
                        acc.extend(sub)
                        continue
                    nm = _rel_of(n)[0] if _rel_of(n) else None
                    if nm not in cte_map or nm in seen:
                        return [], "star_over_external_relation"
                    sub, err = cols_of(cte_map[nm], seen | {nm})
                    if err:
                        return [], err
                    acc.extend(sub)
                continue
            if isinstance(pexp, exp.Column) and isinstance(pexp.this, exp.Star):
                star_src = pexp.table          # alias, not necessarily the CTE name
            if star_src:
                # map alias -> underlying relation name before the CTE lookup
                nm = next((n for n, a in _scope_sources(sel) if a == star_src), star_src)
                if nm not in cte_map or nm in seen:
                    return [], "star_over_external_relation"
                sub, err = cols_of(cte_map[nm], seen | {nm})
                if err:
                    return [], err
                acc.extend(sub)
                continue
            acc.append(pexp.alias_or_name or pexp.sql()[:40])
        return acc, None

    sel = _final_select(tree)
    if sel is None:
        return [], "set_operation"
    return cols_of(sel, frozenset())


def _upstream_joins(sel, cte_map, seen=None) -> bool:
    """Does any scope feeding this one contain joins we have not analysed?

    The grain walk stops at the scope that fixes grain, so a fan-out confined
    to an earlier CTE -- join in one scope, aggregate in the next, which is the
    commonest dbt spelling -- is invisible to it. Reporting an empty shape
    there would be affirmative evidence of absence, so E3 must refuse instead.
    """
    seen = seen if seen is not None else set()
    for n in _source_nodes(sel):
        inner = None
        if isinstance(n, exp.Subquery):
            inner = _select_of(n)
        else:
            r = _rel_of(n)
            nm = r[0] if r else None
            if nm in cte_map and nm not in seen:
                seen.add(nm)
                inner = _select_of(cte_map[nm])
        if inner is None:
            continue
        if _arg(inner, "joins"):
            return True
        if _upstream_joins(inner, cte_map, seen):
            return True
    return False


def _body_tables(sel):
    """Tables referenced by a select's BODY, excluding its WITH definitions."""
    w = _arg(sel, "with_", "with")
    skip = {id(t) for t in w.find_all(exp.Table)} if w is not None else set()
    return [t for t in sel.find_all(exp.Table) if id(t) not in skip]


def _reachable_relations(sel, cte_map, seen=None, acc=None):
    """External relations reachable from THIS select, not every Table in the file.

    A whole-tree scan credits a dead CTE's sources to the output's provenance,
    which is the same wrong-scope pattern the grain walk was fixed for.
    """
    seen = seen if seen is not None else set()
    acc = acc if acc is not None else set()
    for n in _body_tables(sel):
        nm = n.name
        if not nm:
            continue
        # the dbt idiom `with x as (select * from x)`: the inner x is external
        enclosing = None
        anc = n.parent
        while anc is not None:
            if isinstance(anc, exp.CTE):
                enclosing = anc.alias_or_name
                break
            anc = anc.parent
        if nm in cte_map and enclosing != nm:
            if nm in seen:
                continue
            seen.add(nm)
            inner = _select_of(cte_map[nm])
            if inner is not None:
                _reachable_relations(inner, cte_map, seen, acc)
            continue
        parts = [p.name for p in (n.args.get("catalog"), n.args.get("db")) if p]
        acc.add(".".join(parts + [nm]))
    return acc


def analyse(tree) -> dict:
    ctes = _cte_map(tree)
    ev: dict[str, Any] = {}
    ev["set_operation"] = isinstance(tree, (exp.Union, exp.Except, exp.Intersect))
    ev["ctes"] = sorted(ctes)

    # --- source relations reachable from the final select, keeping catalog
    # and schema qualification. A non-recursive CTE cannot reference itself,
    # so in `with x as (select * from x)` the inner x is the EXTERNAL relation.
    _fs = _final_select(tree)
    ev["source_relations"] = sorted(_reachable_relations(_fs, ctes)) if _fs is not None else []

    # --- grain-determining scope, then everything else read from THAT scope
    gsel, construct, grain_refusal = _grain_scope(tree, ctes)
    ev["grain_construct"] = construct
    ev["grain_refusal"] = grain_refusal
    ev["group_keys"] = [e.sql() for e in (_arg(gsel, "group").expressions
                                          if gsel is not None and _arg(gsel, "group") else [])]

    joins = _scope_joins(gsel) if gsel is not None else []
    ev["joins"] = [{k: j[k] for k in ("relation", "type", "keys_text")} for j in joins]
    ev["join_defects"] = {
        "no_condition": sum(j["no_condition"] for j in joins),
        "non_equality": sum(j["non_equality"] for j in joins),
        "unqualified_key": sum(j["unqualified_key"] for j in joins),
        "cross": sum(j.get("cross", False) for j in joins),
    }
    ev["fanout_shape"] = _fanout(joins)

    # --- columns projected from any relation in a shared-key set.
    # Which member is the parent is the contract's call, so naming only the
    # shared-key relation's columns would assert an interpretation and can
    # miss the measure entirely on an equality chain.
    dup, star_in_scope = [], False
    if ev["fanout_shape"] and gsel is not None:
        risky = {k.split(".")[0] for k in ev["fanout_shape"]}
        for parts in ev["fanout_shape"].values():
            risky.update(parts)
        for pexp in gsel.expressions:
            if isinstance(pexp, exp.Star):
                star_in_scope = True
            for col in pexp.find_all(exp.Column):
                if isinstance(col.this, exp.Star):
                    if col.table in risky:
                        star_in_scope = True
                    continue
                if col.table in risky:
                    dup.append(f"{col.table}.{col.name}")
    ev["columns_at_multiplied_grain"] = sorted(set(dup))
    ev["fanout_columns_hidden_by_star"] = star_in_scope
    ev["upstream_joins_unanalysed"] = (_upstream_joins(gsel, ctes)
                                       if gsel is not None else False)

    # --- aggregation and time (time is a NAME heuristic, nothing more)
    ev["aggregate_functions"] = sorted({f.sql_name() for f in tree.find_all(exp.AggFunc)})
    ev["window_functions"] = sorted({w.this.sql_name() for w in tree.find_all(exp.Window)
                                     if isinstance(w.this, exp.Func)})
    t = {c.name for c in tree.find_all(exp.Column)
         if re.search(r"(_at|_date|_time|timestamp|_day|_ts)$", c.name, re.I)}
    ev["time_columns"] = sorted(t)
    ev["has_time_filter"] = any(
        re.search(r"(_at|_date|_time|timestamp|_day|_ts)$", c.name, re.I)
        for w in tree.find_all(exp.Where) for c in w.find_all(exp.Column))

    outs, star_err = resolve_output_columns(tree, ctes)
    ev["output_columns"] = outs
    ev["unresolved_star"] = star_err
    ev["projects_star"] = star_err is not None
    return ev


def evidence_questions(ev: dict) -> dict[str, dict]:
    """Each question is ANSWERED or REFUSED, with the reason recorded."""
    q: dict[str, dict] = {}

    if ev["grain_refusal"]:
        q["E1_grain_construct"] = {"status": "REFUSED", "reason": ev["grain_refusal"]}
    else:
        q["E1_grain_construct"] = {"status": "ANSWERED",
                                   "evidence": {"construct": ev["grain_construct"],
                                                "group_keys": ev["group_keys"]}}

    d = ev["join_defects"]
    if d.get("cross"):
        q["E2_join_structure"] = {"status": "REFUSED", "reason": "cross_join_unkeyed"}
    elif ev["set_operation"]:
        q["E2_join_structure"] = {"status": "REFUSED", "reason": "set_operation_arms_not_reconciled"}
    elif d["no_condition"]:
        q["E2_join_structure"] = {"status": "REFUSED", "reason": "join_without_on_condition"}
    elif d["unqualified_key"]:
        q["E2_join_structure"] = {"status": "REFUSED", "reason": "join_key_not_qualified"}
    elif d["non_equality"]:
        q["E2_join_structure"] = {"status": "REFUSED", "reason": "join_condition_not_equality"}
    else:
        q["E2_join_structure"] = {"status": "ANSWERED",
                                  "evidence": {"joins": ev["joins"],
                                               "fanout_shape": ev["fanout_shape"]}}

    if ev["set_operation"]:
        q["E3_fanout_measure"] = {"status": "REFUSED", "reason": "set_operation_arms_not_reconciled"}
    elif q["E2_join_structure"]["status"] == "REFUSED":
        q["E3_fanout_measure"] = {"status": "REFUSED", "reason": "join_structure_unavailable"}
    elif ev["projects_star"]:
        q["E3_fanout_measure"] = {"status": "REFUSED",
                                  "reason": f"star_unresolved:{ev['unresolved_star']}"}
    elif ev["upstream_joins_unanalysed"]:
        q["E3_fanout_measure"] = {"status": "REFUSED",
                                  "reason": "joins_upstream_of_grain_scope"}
    elif ev["fanout_columns_hidden_by_star"]:
        q["E3_fanout_measure"] = {"status": "REFUSED",
                                  "reason": "fanout_columns_hidden_by_star"}
    else:
        q["E3_fanout_measure"] = {"status": "ANSWERED",
                                  "evidence": {"fanout_shape": ev["fanout_shape"],
                                               "columns_at_multiplied_grain":
                                                   ev["columns_at_multiplied_grain"]}}

    q["E4_time_columns"] = {"status": "ANSWERED",
                            "evidence": {"time_columns": ev["time_columns"],
                                         "has_time_filter": ev["has_time_filter"],
                                         "window_functions": ev["window_functions"]}}

    if ev["set_operation"]:
        q["E5_relation_lineage"] = {"status": "REFUSED",
                                    "reason": "set_operation_arms_not_reconciled"}
    elif ev["projects_star"]:
        q["E5_relation_lineage"] = {"status": "REFUSED",
                                    "reason": f"star_unresolved:{ev['unresolved_star']}"}
    else:
        q["E5_relation_lineage"] = {"status": "ANSWERED",
                                    "evidence": {"source_relations": ev["source_relations"],
                                                 "output_columns": ev["output_columns"]}}
    return q


QUESTIONS = ["E1_grain_construct", "E2_join_structure", "E3_fanout_measure",
             "E4_time_columns", "E5_relation_lineage"]


def run(raw_sql: str) -> dict:
    sql, refuse, detail = resolve_templating(raw_sql)
    if refuse:
        return {"stage": "templating", "refused_all": True,
                "reason": refuse, "detail": detail,
                "questions": {k: {"status": "REFUSED", "reason": f"{refuse}:{detail}"}
                              for k in QUESTIONS}}
    tree, dialect, err = parse(sql)
    if tree is None:
        return {"stage": "parse", "refused_all": True,
                "reason": "parse_error", "detail": err,
                "questions": {k: {"status": "REFUSED", "reason": "parse_error"}
                              for k in QUESTIONS}}
    ev = analyse(tree)
    return {"stage": "analysed", "refused_all": False, "dialect": dialect,
            "evidence": ev, "questions": evidence_questions(ev)}


if __name__ == "__main__":
    import sys
    print(json.dumps(run(open(sys.argv[1]).read()), indent=1, default=str))
