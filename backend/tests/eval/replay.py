# Eval replay - re-runs REAL logged prompts through the normal gateway
# to measure how the CURRENT model and settings handle them. No fixture
# authoring: the corpus is what the game actually sent.
#
# Every replayed request:
#   - goes through ai/gateway.py like any other generation (logged
#     byte-exact, provider-stamped, queued),
#   - runs at priority 10 so live gameplay always wins the queue,
#   - is tagged prompt_type='eval:<original>' so game statistics and
#     eval statistics never mix (report.py splits on that prefix).

from typing import Any, Optional

from .report import EVAL_PREFIX, aggregate, fetch_records, render_table, template_catalog


def select_source_rows(names: list[str], per_template: int) -> dict[str, list]:
    """The latest N completed GAME generations per template - the most
    recent prompts are the ones closest to what the game sends today."""

    from backend.models.core import db
    from backend.models.generation_log import GenerationLog

    sources = {}
    for name in names:
        rows = (
            GenerationLog.query.options(db.joinedload(GenerationLog.llm_log))
            .filter(
                GenerationLog.generation_type == 'llm',
                GenerationLog.status == 'completed',
                GenerationLog.prompt_name == name,
                ~GenerationLog.prompt_type.like(f'{EVAL_PREFIX}%'),
            )
            .order_by(GenerationLog.created_at.desc())
            .limit(per_template)
            .all()
        )
        if rows:
            sources[name] = rows
    return sources


def strip_nothink_prefill(prompt_text: str) -> str:
    """Stored prompt_text is byte-exact and already ENDS with the
    nothink prefill (when the row ran on local) - the gateway appends it
    again per the current provider, so replaying without stripping would
    double it."""

    from backend.core.config.llm_config import NOTHINK_PREFILL

    if prompt_text.endswith(NOTHINK_PREFILL):
        return prompt_text[: -len(NOTHINK_PREFILL)]
    return prompt_text


def _inference_parameters(row, use_current: bool) -> dict[str, Any]:
    """'logged' replays the row's exact parameters (measure the past);
    'current' uses the template's config as it stands (measure a diet
    before soaking it)."""

    if not use_current:
        return row.llm_log.get_inference_params()

    catalog = template_catalog()
    known = catalog.get(row.prompt_name)
    if known is None:
        # Retired template - its current config does not exist; fall
        # back to the logged parameters rather than inventing some
        return row.llm_log.get_inference_params()
    return {'max_tokens': known['max_tokens']}


def run_replay(arguments) -> int:
    names = _resolve_names(arguments)
    if not names:
        print('Nothing to replay - pass --name or --category.')
        return 1

    sources = select_source_rows(names, arguments.per_template)
    if not sources:
        print('No completed game generations found for the selected template(s).')
        return 1

    total_calls = sum(len(rows) for rows in sources.values()) * arguments.runs
    print(
        f'Replaying {sum(len(rows) for rows in sources.values())} logged prompt(s) '
        f'x {arguments.runs} run(s) = {total_calls} generation(s), priority 10.'
    )
    for name, rows in sorted(sources.items()):
        print(f'    {name}: {len(rows)} prompt(s)')

    if arguments.dry_run:
        print('Dry run - no generations sent.')
        return 0

    generation_ids, errored = _replay_all(sources, arguments)

    print(f'\nCompleted {len(generation_ids)} generation(s), {errored} error(s).')
    if not generation_ids:
        return 1

    # The queue worker wrote the results on its OWN connection; under
    # MySQL's REPEATABLE READ this session's open snapshot would still
    # see the rows as pending/empty (the Set-M4 soak-fix precedent).
    # Rolling back ends the snapshot so the fetch below reads reality.
    from backend.models.core import db

    db.session.rollback()

    fresh = fetch_records(generation_ids=generation_ids)
    print(render_table(aggregate(fresh)))
    return 0


def _replay_all(sources: dict[str, list], arguments) -> tuple[list[int], int]:
    """Serial on purpose: the AI queue has one worker (one GPU, one
    model), so firing requests concurrently would only obscure latency"""

    from backend.ai.gateway import text_generation_request

    generation_ids: list[int] = []
    errored = 0
    for name, rows in sorted(sources.items()):
        for row in rows:
            for _ in range(arguments.runs):
                try:
                    result = text_generation_request(
                        prompt=strip_nothink_prefill(row.prompt_text),
                        prompt_type=f'{EVAL_PREFIX}{row.prompt_type}',
                        prompt_name=row.prompt_name,
                        parser_config=row.llm_log.parser_config,
                        priority=10,
                        **_inference_parameters(row, arguments.params == 'current'),
                    )
                    generation_ids.append(result['generation_id'])
                    print(f'    {name}: generation {result["generation_id"]} done')
                except Exception as error:
                    # The failed row still exists in the log table; the
                    # scoreboard below only covers completed replays
                    errored += 1
                    print(f'    {name}: FAILED - {error}')
    return generation_ids, errored


def _resolve_names(arguments) -> Optional[list[str]]:
    if arguments.name:
        return [part.strip() for part in arguments.name.split(',') if part.strip()]
    if arguments.category:
        catalog = template_catalog()
        return [name for name, meta in catalog.items() if meta['category'] == arguments.category]
    return None
