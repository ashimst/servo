# Recipes

Short, tested procedures for common tasks, written in Servo's command grammar.
System 2 gets the best-matching recipes (up to 3) in its planning prompt, chosen by
`triggers`: words or phrases that appear in the request, and `apps`: process names of
the active window.

Format: frontmatter (`id`, `title`, `triggers`, optional `apps`), then the procedure.
Keep each under ~25 lines and use the exact commands from the grammar. Add your own by
dropping a new `.md` file here; it is picked up on the next request, no rebuild needed.
