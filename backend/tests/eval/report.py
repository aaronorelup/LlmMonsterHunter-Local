# Eval report - the per-template scoreboard, computed from the log
# tables alone (zero generations). llm_logs already stores response
# tokens, caps, parse results, speed, provider, and model per request;
# generation_logs adds duration and attempt counts - so "how does the
# model handle each template" is a database question.
#
# Replay rows are tagged prompt_type='eval:<original>' at request time;
# game statistics exclude them by default so soaks and eval runs never
# contaminate each other.

import json
from typing import Any, Optional

EVAL_PREFIX = 'eval:'


# ===== TEMPLATE METADATA (category + current cap, from the live engine) =====


def template_catalog() -> dict[str, dict[str, Any]]:
    """name -> {category, max_tokens} for every template the game can
    send TODAY. Log rows with unknown names belong to retired templates."""

    from backend.ai.llm.prompt_engine import get_prompt_engine

    engine = get_prompt_engine()
    catalog = {}
    for name in engine.list_templates():
        config = engine.get_template_config(name)
        catalog[name] = {
            'category': config['category'],
            'max_tokens': config['max_tokens'],
        }
    return catalog


# ===== FETCH (log rows -> plain records) =====


def fetch_records(
    since=None,
    name: Optional[str] = None,
    category: Optional[str] = None,
    provider: Optional[str] = None,
    eval_only: bool = False,
    generation_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    """Pull LLM generation rows as plain dicts (one per generation).
    Detached from the session on purpose - aggregation is pure Python."""

    from backend.models.core import db
    from backend.models.generation_log import GenerationLog

    catalog = template_catalog()

    query = GenerationLog.query.options(db.joinedload(GenerationLog.llm_log)).filter(
        GenerationLog.generation_type == 'llm'
    )
    if generation_ids is not None:
        query = query.filter(GenerationLog.id.in_(generation_ids))
    elif eval_only:
        query = query.filter(GenerationLog.prompt_type.like(f'{EVAL_PREFIX}%'))
    else:
        query = query.filter(~GenerationLog.prompt_type.like(f'{EVAL_PREFIX}%'))

    if since is not None:
        query = query.filter(GenerationLog.created_at >= since)
    if name:
        query = query.filter(GenerationLog.prompt_name == name)

    records = []
    for row in query.all():
        llm = row.llm_log
        if llm is None:
            continue

        known = catalog.get(row.prompt_name)
        record = {
            'name': row.prompt_name,
            'category': known['category'] if known else '(retired)',
            'current_cap': known['max_tokens'] if known else None,
            'status': row.status,
            'attempts': row.generation_attempt or 1,
            'duration_seconds': row.duration_seconds,
            'max_tokens': llm.max_tokens,
            'response_tokens': llm.response_tokens,
            'tokens_per_second': llm.tokens_per_second,
            'parser_type': (llm.parser_config or {}).get('type'),
            'parse_success': llm.parse_success,
            'provider': llm.provider,
            'model_name': llm.model_name,
        }
        if category and record['category'] != category:
            continue
        if provider and record['provider'] != provider:
            continue
        records.append(record)

    return records


# ===== AGGREGATE (records -> per-template stats) =====


def aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records by template name and compute the scoreboard row for
    each. Percentages are None (rendered '-') when their denominator is
    empty - a template with no JSON parser has no parse-fail rate."""

    by_name: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_name.setdefault(record['name'], []).append(record)

    stats = []
    for name, group in by_name.items():
        completed = [r for r in group if r['status'] == 'completed']
        json_parsed = [r for r in completed if r['parser_type'] == 'json']
        with_tokens = [r for r in completed if r['response_tokens'] is not None]
        durations = [r['duration_seconds'] for r in completed if r['duration_seconds']]
        speeds = [r['tokens_per_second'] for r in completed if r['tokens_per_second']]

        token_counts = sorted(r['response_tokens'] for r in with_tokens)
        truncated = [r for r in with_tokens if r['response_tokens'] >= r['max_tokens']]

        stats.append(
            {
                'name': name,
                'category': group[0]['category'],
                'runs': len(group),
                'failed_percent': _percent(
                    sum(1 for r in group if r['status'] == 'failed'), len(group)
                ),
                'retry_percent': _percent(sum(1 for r in group if r['attempts'] > 1), len(group)),
                'parse_fail_percent': _percent(
                    sum(1 for r in json_parsed if not r['parse_success']), len(json_parsed)
                ),
                'truncated_percent': _percent(len(truncated), len(with_tokens)),
                'average_tokens': _mean(token_counts),
                'p95_tokens': _p95(token_counts),
                'current_cap': group[0]['current_cap'],
                'average_seconds': _mean(durations),
                'tokens_per_second': _mean(speeds),
            }
        )

    stats.sort(key=lambda s: (s['category'], s['name']))
    return stats


def _percent(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def _mean(values) -> Optional[float]:
    values = list(values)
    return round(sum(values) / len(values), 1) if values else None


def _p95(sorted_values) -> Optional[int]:
    if not sorted_values:
        return None
    index = int(0.95 * (len(sorted_values) - 1))
    return sorted_values[index]


# ===== RENDER =====

_COLUMNS = [
    ('category', 'category', 12),
    ('name', 'template', 28),
    ('runs', 'runs', 5),
    ('failed_percent', 'fail%', 6),
    ('retry_percent', 'retry%', 7),
    ('parse_fail_percent', 'parseX%', 8),
    ('truncated_percent', 'trunc%', 7),
    ('average_tokens', 'avg_tok', 8),
    ('p95_tokens', 'p95_tok', 8),
    ('current_cap', 'cap_now', 8),
    ('average_seconds', 'sec/run', 8),
    ('tokens_per_second', 'tok/s', 7),
]


def render_table(stats: list[dict[str, Any]]) -> str:
    """Fixed-width scoreboard, one line per template, grouped by category"""

    header = '  '.join(title.ljust(width) for _, title, width in _COLUMNS)
    lines = [header, '-' * len(header)]
    for row in stats:
        lines.append('  '.join(_cell(row[key], width) for key, _, width in _COLUMNS))
    lines.append('-' * len(header))
    lines.append(f'{len(stats)} template(s), {sum(row["runs"] for row in stats)} generation(s)')
    return '\n'.join(lines)


def _cell(value, width: int) -> str:
    text = '-' if value is None else str(value)
    return text.ljust(width)


# ===== ENTRY (called by __main__) =====


def run_report(arguments) -> int:
    """Fetch, aggregate, print (and optionally write JSON). Returns an
    exit code - 1 when nothing matched, so scripts can tell silence
    from an empty database."""

    records = fetch_records(
        since=arguments.since,
        name=arguments.name,
        category=arguments.category,
        provider=arguments.provider,
        eval_only=arguments.eval_only,
    )
    if not records:
        print('No matching LLM generations found.')
        return 1

    stats = aggregate(records)
    print(render_table(stats))

    if arguments.json:
        with open(arguments.json, 'w', encoding='utf-8') as handle:
            json.dump(stats, handle, indent=2)
        print(f'Wrote {arguments.json}')
    return 0
