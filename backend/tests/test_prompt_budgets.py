# Prompt Budget Tests - OFFLINE (pure template + policy checks, no DB)
# Enforces the text-diet policy (docs/plans/text-diet.md): every prompt
# template declares a budget class in prompt_budgets.py, the map names
# only real templates, and no template's max_tokens exceeds its class
# ceiling. A new template that forgets to declare a class fails here -
# the file-size-ceiling precedent, applied to prompts.
#
# Usage: python -m backend.tests.test_prompt_budgets   (from project root)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = ''):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}{f' - {detail}' if detail else ''}")


def main():
    from backend.ai.llm.prompt_budgets import BUDGET_CLASSES, TEMPLATE_CLASS, ceiling_for
    from backend.ai.llm.prompt_engine import get_prompt_engine

    print('🧪 PROMPT BUDGET TESTS')
    print('=' * 50)

    engine = get_prompt_engine()
    template_names = engine.list_templates()

    print('\n-- every template declares a budget class --')
    check('templates loaded', len(template_names) > 0, 'prompt engine found no templates')
    unmapped = sorted(name for name in template_names if name not in TEMPLATE_CLASS)
    check(
        'every template is mapped in TEMPLATE_CLASS',
        not unmapped,
        f'declare a class in prompt_budgets.py for: {unmapped}',
    )

    print('\n-- the map names only real templates and real classes --')
    ghost_entries = sorted(name for name in TEMPLATE_CLASS if name not in template_names)
    check(
        'no ghost entries (renamed or deleted templates)',
        not ghost_entries,
        f'not in any prompt file: {ghost_entries}',
    )
    unknown_classes = sorted({cls for cls in TEMPLATE_CLASS.values() if cls not in BUDGET_CLASSES})
    check('every declared class exists', not unknown_classes, f'unknown: {unknown_classes}')

    print('\n-- every max_tokens sits under its class ceiling --')
    over_ceiling = []
    for name in template_names:
        ceiling = ceiling_for(name)
        if ceiling is None:
            continue  # already reported as unmapped above
        cap = engine.get_template_config(name)['max_tokens']
        if cap > ceiling:
            over_ceiling.append(f'{name} ({cap} > {ceiling})')
    check('no template exceeds its ceiling', not over_ceiling, '; '.join(over_ceiling))

    print('\n' + '=' * 50)
    print(f'PASSED {PASSED}  FAILED {FAILED}')
    return FAILED


if __name__ == '__main__':
    raise SystemExit(main())
