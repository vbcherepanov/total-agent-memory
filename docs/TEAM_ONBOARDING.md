# Department onboarding on the team server

A new employee joins a department (a team on the team server), types `/onboard` in their agent (Claude Code, Codex or any MCP client), and works through what the department already knows. The server remembers what they studied and when, and it grades their quizzes. The department head gets the results as a report.

## Concept

- **Curriculum**: one per team. It holds ordered **modules**, and each module holds ordered **lessons**. A lesson has its own text (`body`), IDs of records in the team's memory (`record_ids`), or both.
- **Lesson versions**: when a lesson is served, the server resolves each referenced record to its current version (following `memory_update` supersession) and hashes the title, the text and the source contents. A completed lesson keeps the hash the employee studied. If the hash changes later, the lesson is flagged `updated_since_studied` and offered again for review.
- **Quiz**: one per module. Question types are `single`, `multiple` and `open`. Choice questions are graded deterministically: full points on an exact match with the answer key, otherwise zero. Open answers are graded by the server LLM when one is configured. Otherwise they wait in the department head's grading queue. Each module has its own pass threshold (default `0.8`) and attempt limit (default `3`, maximum `20`). Every attempt is stored with a snapshot of the questions it answered.
- **Progress**: tracked per user and lesson: `opened_at`, `completed_at`, `first_completed_at`, `time_spent_seconds` (from the first open to the first completion) and the studied hash/version. Quiz progress is tracked per attempt: score, status and timestamps.
- **Learning log**: a team-scoped event list, for example "Vasya completed lesson 'Pipeline' (module 'Deploy') on 2026-09-25" or "Vasya took the quiz of module 'Deploy' on 2026-09-25: 8/10 (80%), passed".
- **Personal notes**: short notes about completed lessons and quiz results go into the employee's own personal memory (project `onboarding`, tags `onboarding`, `<team_id>`). A later `memory_recall` by that employee finds them.

Storage: `learning.db` (SQLite, mode 0600) in the team server root, next to `identity.db`. Migrations are versioned and idempotent. `tam-team backup` and `restore` include `learning.db`.

## Roles

| Actor | Study | Own progress | Others' progress / team report | Grading queue | Edit curriculum and quizzes |
|---|---|---|---|---|---|
| team `reader` / `editor` | own team | yes | no | no | no |
| team `manager` (department head) | own team | yes | own team | own team | own team |
| org `company_viewer` | teams they belong to | yes | all teams | no | no |
| org `superadmin` | teams they belong to | yes | all teams | all teams | all teams |

The service layer enforces authorization for MCP, `/api/call` and the dashboard's `/learning/api/*` routes alike. The UI only hides controls. Set roles with `tam-team member <user> <team> manager` and `tam-team user-role <user> company_viewer|superadmin|member` (see [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md)).

## Manager workflow

1. Run `/onboard-report <team>` in the agent, or open **Onboarding → Department** in the dashboard.
2. Build the first curriculum with `onboarding_curriculum_draft(team_id)`. The draft groups the team's active records by project (for `general`, by the first topical tag) and then by type, in the fixed order conventions → decisions → solutions → lessons → facts, with 8 records per lesson at most. The same memory always produces the same draft. If an LLM is configured (see [Server LLM](#server-llm)), it only rewrites titles, summaries and lesson introductions. Record references stay deterministic, and when the model fails the draft falls back to the deterministic version and reports `llm_error`. The draft is not saved.
3. Review and edit, then save with `onboarding_curriculum_set(team_id, expected_revision, curriculum)`. A stale `expected_revision` is rejected. Record IDs are validated against team memory. Keep existing module and lesson `id`s so employees keep their progress. Omitted items are archived, not deleted.
4. Write quizzes with `onboarding_quiz_set(module_id, expected_revision, questions)`, or generate them with `onboarding_quiz_draft(module_id)`, which needs an LLM. Each generated question must name one of the module's lessons and quote at least three words of it verbatim. Questions without such a quote are listed under `rejected`. Drafts are not saved.
5. Follow up with `onboarding_team_report(team_id)`: each member against each module, with status, lessons done, best score, attempts and dates, plus the recent learning log. For department heads it also includes the grading queue. Grade open answers with `onboarding_grade(attempt_id, question_id, score, comment)`. Grading the last pending answer finalizes the attempt. Grading an already finalized answer recomputes the result.

## Employee workflow

1. `/onboard` → `onboarding_overview` lists the employee's departments → `onboarding_start(team_id)` enrolls them and returns the plan and the next step.
2. `onboarding_next(team_id)` opens the next unfinished lesson, or `lesson_id` reopens a specific one. The lesson comes with its source records inline. The agent summarizes it and answers questions with `memory_recall` in the team scope.
3. `onboarding_complete(lesson_id)` marks the lesson studied. Completion requires that the lesson was opened first. A repeated call returns `unchanged`. Completing a lesson again after its sources changed returns `restudied`.
4. Once all lessons of a module are completed, `onboarding_quiz(module_id)` returns the questions without answer keys or rubrics, and `onboarding_submit(module_id, answers)` grades them. Correct options are shown only after a pass or after the last attempt. Submission is refused while an attempt waits for review, after the module is passed, and once no attempts are left.
5. `onboarding_progress(team_id)` shows the employee's own progress and log.

## Tools reference

| Tool | Who | Purpose |
|---|---|---|
| `onboarding_overview()` | anyone | Your teams with curriculum and progress summary; the teams you can view or manage |
| `onboarding_start(team_id)` | member | Enroll or resume; returns the plan and the next step |
| `onboarding_next(team_id, lesson_id?)` | member | Open a lesson with its records inline |
| `onboarding_complete(lesson_id)` | member | Mark a lesson studied (idempotent) |
| `onboarding_quiz(module_id)` | member | Questions without answers |
| `onboarding_submit(module_id, answers)` | member | Grade an attempt; `answers=[{question_id, choices:[int]} or {question_id, text}]` |
| `onboarding_progress(team_id, user_id?)` | member (self); head, viewer, superadmin (others) | Progress and log |
| `onboarding_team_report(team_id)` | head, viewer, superadmin | Members × modules report, log, grading queue (heads) |
| `onboarding_curriculum_get(team_id)` | head, superadmin (answer keys); viewer (no keys) | Curriculum with revisions |
| `onboarding_curriculum_set(team_id, expected_revision, curriculum)` | head, superadmin | Replace the curriculum |
| `onboarding_curriculum_draft(team_id)` | head, superadmin | Deterministic draft from team memory |
| `onboarding_quiz_set(module_id, expected_revision, questions)` | head, superadmin | Replace a quiz |
| `onboarding_quiz_draft(module_id)` | head, superadmin | LLM draft checked against lesson quotes |
| `onboarding_grade(attempt_id, question_id, score, comment)` | head, superadmin | Grade an open answer |

Errors use the gateway's codes: `forbidden`, `conflict` (wrong state or stale revision), `unavailable` (no LLM configured, or a workspace process is unavailable) and `invalid_request`.

## Dashboard

The **Onboarding** section (group *Personal*, icon *book*) is registered for every signed-in user by `team_memory.learning.api.register_section()` and mounted with `TamLearning.mount(element, ctx)`. It is built only from `ctx.ui` components, under the dashboard CSP (`script-src 'self'`, no inline script or style). It has three tabs:

- **My courses** (members): one card per department with a progress ring, the next lesson and the `/onboard <team>` command to continue in the agent, modules with lesson status and quiz results, and a flag on lessons updated since they were studied.
- **Department** (department heads, company viewers, superadmins): completion, finished, average score and grading KPIs, a members × modules matrix with status chips, scores and dates, and the learning log. Department heads also see the grading queue and the editors:
    - **Curriculum editor**: add, reorder and remove modules and lessons, set each module's quiz pass threshold and attempt limit, and pick source records from team memory with search. **Draft from team memory** fills the editor, and **Publish** saves it with the revision check.
    - **Quiz editor**, one per module: single-choice, multiple-choice and open questions, correct-option toggles, points, a related lesson, and rubrics for open questions. **Draft with LLM** is also available.
    - Both editors validate on the client (required fields, option and answer-key rules, limits), and the server validates the same rules again.
- **Company** (company viewers, superadmins): the completion rate, the number enrolled and finished, the average score and the pending grading count for each department.

Session API: `POST /learning/api/<action>` calls the matching `onboarding_<action>` tool as the signed-in user (cookie and `X-CSRF-Token`). The worker credential re-validates the session on every call. `GET /learning/api/kpis/team/{team_id}` requires the head of that department, a company viewer or a superadmin. `GET /learning/api/kpis/company` requires a company viewer or a superadmin. The overview page shows these numbers as an *Onboarding completion* tile for department heads and for company roles.

## Server LLM

Open-answer grading, curriculum polishing and `onboarding_quiz_draft` run in the gateway process. They use the LLM provider settings from the dashboard (*Administration → Provider settings*) with the same precedence as the workspace workers: dashboard value > the server's environment / `.env` > built-in default (`MEMORY_LLM_*` names, see [llm-settings.md](llm-settings.md)). The gateway resolves the settings on every call and builds the provider from that explicit configuration. It never writes them into `os.environ`, so concurrent requests cannot see each other's values, and a saved change applies to the next call without a restart. With `MEMORY_LLM_ENABLED=auto` the gateway checks that the provider is reachable (for Ollama, that the model is installed) and caches the answer for 60 s per configuration. Logs name the provider and model and whether a key is set, never the key.

## Privacy model

- Personal memory stays private. The server writes notes into the employee's personal workspace with the employee's own token only, through a per-user outbox. If the workspace is unavailable, the note is retried on the employee's next onboarding call, and it is idempotent by request ID. Managers never read personal memory, and learning data is never written to team memory, where every team member (readers too) could recall a colleague's scores.
- Only the department head of that team, company viewers and superadmins can see who studied what and how they scored. They get it through `onboarding_team_report` and `onboarding_progress`, which read `learning.db`. The grading queue with answer texts is limited to people who can grade.
- Quiz answer keys and rubrics are returned only to people who can manage the curriculum. The LLM grader receives the rubric, the lesson material and the answer. The answer is marked as untrusted and the model is told to ignore instructions inside it. The department head can regrade any open answer.
- Structured logs record event names and IDs, never answer text or tokens. Tool calls are counted and timed by the gateway's existing metrics, per tool name.
