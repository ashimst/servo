# Recipes

Short, tested procedures for common tasks, written in Servo's step forms (`STEP_FORMS` in
`servo/cognition/specialists.py`). The planner gets the best-matching recipes (up to 3) in
its prompt, chosen by `triggers` (words or phrases in the request) and `apps` (process names
of the active window).

Format: frontmatter (`id`, `title`, `triggers`, optional `apps`), then the procedure. Keep
each under ~25 lines. Refer to controls as `[n]` (the number in the current screen
description) and use only the step forms; anything else is rejected at run time. Add your
own by dropping a new `.md` file here; it is picked up on the next request.
