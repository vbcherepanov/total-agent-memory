import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from team_memory.contracts import (
    OVERSIGHT_ROLES,
    Conflict,
    DomainError,
    Empty,
    Forbidden,
    Unavailable,
)
from team_memory.learning import drafting, llm, reporting
from team_memory.learning.contracts import (
    CurriculumSet,
    CurriculumSpec,
    GradeRequest,
    LessonRequest,
    ModuleRequest,
    NextRequest,
    ProgressRequest,
    QuizSet,
    QuizSubmit,
    TeamRequest,
)
from team_memory.learning.llm import LLMFactory, LLMFailure
from team_memory.learning.repository import LearningRepository
from team_memory.learning.sources import (
    EXPORT_PAGE,
    NOTE_NAMESPACE,
    Caller,
    MemorySource,
    PoolMemorySource,
    resolve,
)
from team_memory.registry import Registry

LOGGER = logging.getLogger(__name__)
SOURCE_REFRESH_SECONDS = 300
DRAFT_QUESTIONS_PER_LESSON = 2
DRAFT_QUESTIONS_MAX = 20
PERCENT = 100
MANAGER_ROLE = "manager"
SUPERADMIN = "superadmin"
LEARNER_EVENTS = frozenset(("enrolled", "lesson_completed", "lesson_restudied", "quiz_submitted", "quiz_graded",
                            "quiz_regraded"))
QUESTION_PUBLIC_FIELDS = ("id", "type", "prompt", "options", "points", "lesson_id")


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def lesson_material(lesson: dict, records: list[dict]) -> str:
    parts = [lesson["title"], lesson["body"]]
    parts.extend(f"[record {r['id']}] {r.get('content', '')}" for r in records)
    return "\n\n".join(part for part in parts if part)


def lesson_hash(lesson: dict, records: list[dict]) -> str:
    payload = {"title": lesson["title"], "body": lesson["body"],
               "sources": [[r["id"], r.get("content", "")] for r in records]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def public_record(requested_id: int, record: dict) -> dict:
    author = record.get("created_by") or {}
    return {"id": record["id"], "requested_id": requested_id, "type": record.get("type"),
            "project": record.get("project"), "content": record.get("content", ""),
            "author": author.get("display_name"), "revision": record.get("revision")}


def public_question(question: dict) -> dict:
    return {key: question.get(key) for key in QUESTION_PUBLIC_FIELDS}


class LearningService:
    def __init__(self, registry: Registry, repository: LearningRepository, source: MemorySource,
                 llm_factory: LLMFactory, clock: Callable[[], datetime] = utc_now):
        self.registry, self.repository, self.source = registry, repository, source
        self.llm_factory, self.clock = llm_factory, clock
        self.handlers = {
            "onboarding_overview": self.overview, "onboarding_start": self.start, "onboarding_next": self.next,
            "onboarding_complete": self.complete, "onboarding_quiz": self.quiz, "onboarding_submit": self.submit,
            "onboarding_progress": self.progress, "onboarding_team_report": self.team_report,
            "onboarding_curriculum_get": self.curriculum_get, "onboarding_curriculum_set": self.curriculum_set,
            "onboarding_curriculum_draft": self.curriculum_draft, "onboarding_quiz_set": self.quiz_set,
            "onboarding_quiz_draft": self.quiz_draft, "onboarding_grade": self.grade,
        }

    @classmethod
    def default(cls, registry: Registry, pool, llm_factory: LLMFactory) -> "LearningService":
        return cls(registry, LearningRepository(registry.root, registry.plane), PoolMemorySource(registry, pool), llm_factory)

    async def call(self, caller: Caller, name: str, request, *, readonly: bool = False) -> JsonValue:
        if name not in self.handlers:
            raise DomainError("Unknown onboarding tool")
        result = await self.handlers[name](caller, request)
        if not readonly:
            await self._deliver_notes(caller)
        return result

    # Authorization

    def _require_member(self, caller: Caller, team_id: str) -> str:
        role = self.registry.team_role(caller.actor.user_id, team_id)
        if role is None:
            raise Forbidden("You are not a member of this department")
        return role

    def _can_manage(self, caller: Caller, team_id: str) -> bool:
        return (self.registry.team_role(caller.actor.user_id, team_id) == MANAGER_ROLE
                or self.registry.org_role(caller.actor.user_id) == SUPERADMIN)

    def _require_manage(self, caller: Caller, team_id: str) -> None:
        if not self._can_manage(caller, team_id):
            raise Forbidden("Only the department head can change onboarding")

    def _require_view_people(self, caller: Caller, team_id: str) -> None:
        if not self.registry.can_view_team_people(caller.actor, team_id):
            raise Forbidden("Only the department head, company viewers and superadmins can view people")

    def _curriculum(self, team_id: str) -> dict:
        curriculum = self.repository.curriculum(team_id)
        if curriculum is None or not curriculum["modules"]:
            raise Conflict("This department has no onboarding curriculum yet; ask the department head to publish one")
        return curriculum

    def _module(self, module_id: str) -> dict:
        module = self.repository.module(module_id)
        if module is None:
            raise Conflict("Module not found")
        return module

    def _now(self) -> str:
        return iso(self.clock())

    # Sources

    async def _lesson_records(self, caller: Caller, team_id: str, lesson: dict) -> tuple[list[dict], list[int]]:
        records, missing = [], []
        for record_id in lesson["record_ids"]:
            record = await resolve(self.source, caller, team_id, record_id)
            if record is None:
                missing.append(record_id)
            else:
                records.append(public_record(record_id, record))
        return records, missing

    async def _refresh(self, caller: Caller, team_id: str, lesson: dict, force: bool) -> tuple[dict, list[dict], list[int]]:
        checked = lesson.get("checked_at")
        fresh = checked is not None and self.clock() - parse(checked) < timedelta(seconds=SOURCE_REFRESH_SECONDS)
        if not lesson["record_ids"] and lesson["content_hash"]:
            return lesson, [], []
        if fresh and not force:
            return lesson, [], []
        records, missing = await self._lesson_records(caller, team_id, lesson)
        content_hash = lesson_hash(lesson, records)
        self.repository.update_lesson_source(lesson["id"], content_hash, [r["id"] for r in records], self._now())
        changed = content_hash != lesson["content_hash"]
        return ({**lesson, "content_hash": content_hash, "version": lesson["version"] + int(changed),
                 "checked_at": self._now()}, records, missing)

    async def _refresh_curriculum(self, caller: Caller, team_id: str, curriculum: dict) -> dict:
        for module in curriculum["modules"]:
            module["lessons"] = [(await self._refresh(caller, team_id, lesson, False))[0] for lesson in module["lessons"]]
        return curriculum

    # Learner tools

    async def overview(self, caller: Caller, _request: Empty) -> JsonValue:
        actor = caller.actor
        org_role = self.registry.org_role(actor.user_id)
        teams = []
        all_teams = self.registry.list_teams()
        names = dict(all_teams)
        for team_id, role in self.registry.teams_of(actor.user_id):
            curriculum = self.repository.curriculum(team_id)
            entry = {"team_id": team_id, "name": names.get(team_id, team_id), "role": role,
                     "has_curriculum": bool(curriculum and curriculum["modules"]),
                     "enrolled_at": self.repository.enrollment(team_id, actor.user_id)}
            if entry["has_curriculum"]:
                entry["title"] = curriculum["title"]
                own = reporting.user_progress(curriculum, actor.user_id,
                                              self.repository.progress(team_id, [actor.user_id]),
                                              self.repository.attempts_for_team_user(team_id, actor.user_id))
                entry["summary"] = own["summary"]
            teams.append(entry)
        viewable = [{"team_id": t, "name": n} for t, n in all_teams if self.registry.can_view_team_people(actor, t)]
        manageable = [{"team_id": t, "name": n} for t, n in all_teams if self._can_manage(caller, t)]
        return {"user": {"user_id": actor.user_id, "display_name": actor.display_name, "org_role": org_role},
                "teams": teams, "viewable_teams": viewable, "manageable_teams": manageable}

    async def start(self, caller: Caller, request: TeamRequest) -> JsonValue:
        self._require_member(caller, request.team_id)
        curriculum = await self._refresh_curriculum(caller, request.team_id, self._curriculum(request.team_id))
        started_at, created = self.repository.enroll(request.team_id, caller.actor.user_id, self._now())
        if created:
            self.repository.log({"team_id": request.team_id, "user_id": caller.actor.user_id, "at": started_at,
                                 "event": "enrolled", "subject_id": request.team_id,
                                 "summary": f"{caller.actor.display_name} started onboarding on {started_at[:10]}"})
        progress = self._own_progress(curriculum, request.team_id, caller.actor.user_id)
        return {"team_id": request.team_id, "title": curriculum["title"], "enrolled_at": started_at,
                "newly_enrolled": created, "plan": progress["modules"], "summary": progress["summary"],
                "next": reporting.next_step(progress)}

    def _own_progress(self, curriculum: dict, team_id: str, user_id: str) -> dict:
        return reporting.user_progress(curriculum, user_id, self.repository.progress(team_id, [user_id]),
                                       self.repository.attempts_for_team_user(team_id, user_id))

    async def next(self, caller: Caller, request: NextRequest) -> JsonValue:
        self._require_member(caller, request.team_id)
        curriculum = self._curriculum(request.team_id)
        user_id = caller.actor.user_id
        self.repository.enroll(request.team_id, user_id, self._now())
        lessons = [(module, lesson) for module in curriculum["modules"] for lesson in module["lessons"]]
        if request.lesson_id is not None:
            chosen = next(((m, lesson) for m, lesson in lessons if lesson["id"] == request.lesson_id), None)
            if chosen is None:
                raise Conflict("Lesson not found in this department's curriculum")
        else:
            progress = self._own_progress(await self._refresh_curriculum(caller, request.team_id, curriculum),
                                          request.team_id, user_id)
            step = reporting.next_step(progress)
            if step["action"] not in ("lesson", "review"):
                return {"lesson": None, "next": step, "summary": progress["summary"]}
            chosen = next((m, lesson) for m, lesson in lessons if lesson["id"] == step["lesson_id"])
        module, lesson = chosen
        lesson, records, missing = await self._refresh(caller, request.team_id, lesson, True)
        row = self.repository.open_lesson(user_id, lesson["id"], self._now())
        status = reporting.lesson_status(lesson, row)
        position = [item[1]["id"] for item in lessons].index(lesson["id"]) + 1
        return {"lesson": {"lesson_id": lesson["id"], "title": lesson["title"], "body": lesson["body"],
                           "version": lesson["version"], "module_id": module["id"], "module_title": module["title"],
                           "module_summary": module["summary"], "position": position, "total_lessons": len(lessons),
                           "records": records, "missing_record_ids": missing},
                "progress": status,
                "instructions": "Teach this lesson from the text and records, answer questions with memory_recall "
                                "in this team scope, then call onboarding_complete with lesson_id."}

    async def complete(self, caller: Caller, request: LessonRequest) -> JsonValue:
        lesson = self.repository.lesson(request.lesson_id)
        if lesson is None:
            raise Conflict("Lesson not found")
        team_id = lesson["team_id"]
        self._require_member(caller, team_id)
        user_id, now = caller.actor.user_id, self.clock()
        opened = self.repository.progress(team_id, [user_id]).get((user_id, lesson["id"]))
        elapsed = 0 if opened is None else max(0, int((now - parse(opened["opened_at"])).total_seconds()))
        stamp = iso(now)
        log = {"team_id": team_id, "user_id": user_id, "at": stamp, "subject_id": lesson["id"],
               "summary": f"{caller.actor.display_name} completed lesson '{lesson['title']}' "
                          f"(module '{lesson['module_title']}') on {stamp[:10]}"}
        note = (f"Onboarding in department {team_id}: completed lesson '{lesson['title']}' of module "
                f"'{lesson['module_title']}' on {stamp[:10]}.")
        row, outcome = self.repository.complete_lesson(user_id, lesson, stamp, elapsed, log, note)
        curriculum = self._curriculum(team_id)
        progress = self._own_progress(curriculum, team_id, user_id)
        return {"lesson_id": lesson["id"], "outcome": outcome, "completed_at": row["completed_at"],
                "time_spent_seconds": row["time_spent_seconds"], "summary": progress["summary"],
                "next": reporting.next_step(progress)}

    def _require_lessons_done(self, module: dict, user_id: str) -> None:
        rows = self.repository.progress(module["team_id"], [user_id])
        pending = [lesson["title"] for lesson in module["lessons"]
                   if (rows.get((user_id, lesson["id"])) or {}).get("completed_at") is None]
        if pending:
            raise Conflict("Complete all lessons of this module first: " + ", ".join(pending))

    async def quiz(self, caller: Caller, request: ModuleRequest) -> JsonValue:
        module = self._module(request.module_id)
        self._require_member(caller, module["team_id"])
        if module["quiz"] is None:
            raise Conflict("This module has no quiz yet")
        self._require_lessons_done(module, caller.actor.user_id)
        attempts = self.repository.attempts(caller.actor.user_id, module["id"])
        status = reporting.quiz_status(module, attempts)
        return {"module_id": module["id"], "title": module["title"], "pass_threshold": module["pass_threshold"],
                "attempts_used": status["attempts_used"], "attempts_left": status["attempts_left"],
                "passed": status["passed"], "pending_review": status["pending_review"],
                "questions": [public_question(q) for q in module["quiz"]["questions"]],
                "instructions": "Ask the employee each question and submit their own answers with onboarding_submit; "
                                "choices are zero-based option indexes."}

    async def submit(self, caller: Caller, request: QuizSubmit) -> JsonValue:
        module = self._module(request.module_id)
        team_id, user_id = module["team_id"], caller.actor.user_id
        self._require_member(caller, team_id)
        quiz = module["quiz"]
        if quiz is None:
            raise Conflict("This module has no quiz yet")
        self._require_lessons_done(module, user_id)
        self.repository.check_attempt_allowed(self.repository.attempts(user_id, module["id"]), module["max_attempts"])
        answers = {answer.question_id: answer for answer in request.answers}
        unknown = set(answers) - {q["id"] for q in quiz["questions"]}
        if unknown:
            raise DomainError("Unknown question ids: " + ", ".join(sorted(unknown)))
        now = self._now()
        graded = await self._grade_answers(caller, module, quiz["questions"], answers, now)
        score = sum(item["score"] for item in graded)
        max_score = float(sum(item["points"] for item in graded))
        pending = any(item["status"] == "pending" for item in graded)
        passed = None if pending else score >= module["pass_threshold"] * max_score
        attempt = {"team_id": team_id, "module_id": module["id"], "user_id": user_id,
                   "quiz_revision": quiz["revision"], "submitted_at": now,
                   "status": "pending_review" if pending else "graded", "score": score, "max_score": max_score,
                   "pass_threshold": module["pass_threshold"], "passed": None if passed is None else int(passed),
                   "graded_at": None if pending else now}
        verdict = self._verdict(score, max_score, passed)
        log = {"team_id": team_id, "user_id": user_id, "at": now, "event": "quiz_submitted",
               "summary": f"{caller.actor.display_name} took the quiz of module '{module['title']}' on {now[:10]}: {verdict}"}
        note = (f"Onboarding in department {team_id}: quiz of module '{module['title']}' on {now[:10]}: {verdict}.")
        attempt_id = self.repository.insert_attempt(attempt, graded, module["max_attempts"], log, note)
        return self._attempt_view(module, attempt_id)

    @staticmethod
    def _verdict(score: float, max_score: float, passed: bool | None) -> str:
        text = f"{score:g}/{max_score:g} ({round(PERCENT * score / max_score) if max_score else 0}%)"
        if passed is None:
            return text + " so far, open answers await review"
        return text + (", passed" if passed else ", not passed")

    async def _grade_answers(self, caller: Caller, module: dict, questions: list[dict], answers: dict, now: str) -> list[dict]:
        needs_llm = any(q["type"] == "open" and q["id"] in answers and answers[q["id"]].text.strip() for q in questions)
        model = await asyncio.to_thread(self.llm_factory) if needs_llm else None
        materials: dict[str | None, str] = {}
        graded = []
        for position, question in enumerate(questions):
            answer = answers.get(question["id"])
            choices = sorted(set(answer.choices)) if answer else []
            text = answer.text.strip() if answer else ""
            item = {"question_id": question["id"], "position": position, "question": question,
                    "answer": {"choices": choices, "text": text}, "points": float(question["points"]),
                    "score": 0.0, "status": "graded", "grader": "auto", "comment": "", "graded_by": None,
                    "graded_at": now}
            if question["type"] != "open":
                if any(index >= len(question["options"]) for index in choices) or (question["type"] == "single" and len(choices) > 1):
                    raise DomainError(f"Invalid choices for question {question['id']}")
                if choices == sorted(question["correct"]):
                    item["score"] = item["points"]
            elif not text:
                item["comment"] = "No answer given"
            elif model is None:
                item.update(status="pending", grader=None, graded_at=None)
            else:
                lesson_id = question.get("lesson_id")
                if lesson_id not in materials:
                    materials[lesson_id] = await self._material(caller, module, lesson_id)
                try:
                    verdict = await asyncio.to_thread(llm.grade_open_answer, model, question, text, materials[lesson_id])
                    item.update(score=round(verdict.score * item["points"], 2), grader="llm", comment=verdict.comment)
                except LLMFailure as exc:
                    LOGGER.warning(json.dumps({"event": "onboarding_llm_grade_failed", "module_id": module["id"],
                                               "question_id": question["id"], "error": str(exc)}))
                    item.update(status="pending", grader=None, graded_at=None)
            graded.append(item)
        return graded

    async def _material(self, caller: Caller, module: dict, lesson_id: str | None) -> str:
        lessons = [lesson for lesson in module["lessons"] if lesson_id is None or lesson["id"] == lesson_id]
        texts = []
        for lesson in lessons:
            records, _ = await self._lesson_records(caller, module["team_id"], lesson)
            texts.append(lesson_material(lesson, records))
        return "\n\n".join(texts)

    def _attempt_view(self, module: dict, attempt_id: int) -> dict:
        attempt = self.repository.attempt(attempt_id)
        attempts = self.repository.attempts(attempt["user_id"], module["id"])
        left = max(0, module["max_attempts"] - len(attempts))
        reveal = attempt["status"] == "graded" and (bool(attempt["passed"]) or left == 0)
        results = []
        for answer in attempt["answers"]:
            question = answer["question"]
            entry = {"question_id": answer["question_id"], "status": answer["status"], "earned": answer["score"],
                     "points": answer["points"], "grader": answer["grader"], "comment": answer["comment"],
                     "correct": None if answer["status"] == "pending" else answer["score"] >= answer["points"]}
            if reveal:
                entry["correct_options"] = question["correct"] if question["type"] != "open" else None
                entry["rubric"] = question["rubric"] if question["type"] == "open" else None
            results.append(entry)
        max_score = attempt["max_score"]
        return {"attempt_id": attempt_id, "module_id": module["id"], "status": attempt["status"],
                "score": attempt["score"], "max_score": max_score,
                "percent": round(PERCENT * attempt["score"] / max_score) if max_score else 0,
                "pass_threshold": attempt["pass_threshold"],
                "passed": None if attempt["passed"] is None else bool(attempt["passed"]),
                "attempts_used": len(attempts), "attempts_left": left, "answers_revealed": reveal,
                "results": results}

    async def progress(self, caller: Caller, request: ProgressRequest) -> JsonValue:
        target = request.user_id or caller.actor.user_id
        if target == caller.actor.user_id:
            self._require_member(caller, request.team_id)
            curriculum = await self._refresh_curriculum(caller, request.team_id, self._curriculum(request.team_id))
        else:
            self._require_view_people(caller, request.team_id)
            if self.registry.team_role(target, request.team_id) is None and \
                    self.repository.enrollment(request.team_id, target) is None:
                raise Forbidden("This person is not in the department")
            curriculum = self._curriculum(request.team_id)
        progress = self._own_progress(curriculum, request.team_id, target)
        return {"team_id": request.team_id, "user_id": target, "title": curriculum["title"],
                "enrolled_at": self.repository.enrollment(request.team_id, target),
                "modules": progress["modules"], "summary": progress["summary"], "next": reporting.next_step(progress),
                "log": [entry for entry in self.repository.recent_log(request.team_id, target)
                        if entry["event"] in LEARNER_EVENTS]}

    async def team_report(self, caller: Caller, request: TeamRequest) -> JsonValue:
        self._require_view_people(caller, request.team_id)
        curriculum = self._curriculum(request.team_id)
        members = self.registry.team_members(request.team_id)
        rows = self.repository.progress(request.team_id)
        attempts = self.repository.team_attempts(request.team_id)
        people = []
        for user_id, name, role in members:
            progress = reporting.user_progress(curriculum, user_id, rows, attempts)
            people.append({"user_id": user_id, "name": name, "role": role,
                           "enrolled_at": self.repository.enrollment(request.team_id, user_id),
                           "summary": progress["summary"],
                           "modules": [{key: module[key] for key in ("module_id", "title", "status", "lessons_completed",
                                                                     "lessons_total", "started_at", "lessons_finished_at", "quiz")}
                                       for module in progress["modules"]]})
        report = {"team_id": request.team_id, "title": curriculum["title"], "generated_at": self._now(),
                  "modules": [{"module_id": m["id"], "title": m["title"]} for m in curriculum["modules"]],
                  "members": people, "log": self.repository.recent_log(request.team_id)}
        if self._can_manage(caller, request.team_id):
            report["grading_queue"] = [
                {"attempt_id": item["attempt_id"], "question_id": item["question_id"], "user_id": item["user_id"],
                 "module_id": item["module_id"], "submitted_at": item["submitted_at"],
                 "prompt": item["question"]["prompt"], "rubric": item["question"]["rubric"],
                 "points": item["points"], "answer": item["answer"]["text"]}
                for item in self.repository.grading_queue(request.team_id)]
        return report

    # Manager tools

    async def curriculum_get(self, caller: Caller, request: TeamRequest) -> JsonValue:
        manager = self._can_manage(caller, request.team_id)
        if not manager:
            self._require_view_people(caller, request.team_id)
        curriculum = self.repository.curriculum(request.team_id)
        if curriculum is None:
            return {"team_id": request.team_id, "revision": 0, "curriculum": None}
        modules = []
        for module in curriculum["modules"]:
            quiz = module["quiz"]
            if quiz is not None and not manager:
                quiz = {**quiz, "questions": [public_question(q) for q in quiz["questions"]]}
            lessons = []
            for lesson in module["lessons"]:
                entry = {"id": lesson["id"], "title": lesson["title"], "body": lesson["body"],
                         "record_ids": lesson["record_ids"], "version": lesson["version"],
                         "checked_at": lesson["checked_at"]}
                if manager:
                    entry["sources"] = await self._source_previews(caller, request.team_id, lesson["record_ids"])
                lessons.append(entry)
            modules.append({"id": module["id"], "title": module["title"], "summary": module["summary"],
                            "pass_threshold": module["pass_threshold"], "max_attempts": module["max_attempts"],
                            "lessons": lessons,
                            "quiz": None if quiz is None else {"revision": quiz["revision"], "questions": quiz["questions"],
                                                               "updated_at": quiz["updated_at"]}})
        return {"team_id": request.team_id, "revision": curriculum["revision"], "updated_at": curriculum["updated_at"],
                "updated_by": curriculum["updated_by"],
                "curriculum": {"title": curriculum["title"], "modules": modules}}

    async def _source_previews(self, caller: Caller, team_id: str, record_ids: list[int]) -> list[dict]:
        previews = []
        for record_id in record_ids:
            record = await resolve(self.source, caller, team_id, record_id)
            previews.append({"requested_id": record_id, "missing": record is None,
                             "id": None if record is None else record["id"],
                             "type": None if record is None else record.get("type"),
                             "project": None if record is None else record.get("project"),
                             "excerpt": "" if record is None else drafting.first_line(record.get("content", ""))})
        return previews

    # Aggregates for dashboard overviews

    def _team_kpis(self, team_id: str) -> dict:
        curriculum = self.repository.curriculum(team_id)
        members = self.registry.team_members(team_id)
        if curriculum is None or not curriculum["modules"]:
            return reporting.empty_kpis(len(members))
        return reporting.team_kpis(curriculum, [user_id for user_id, _, _ in members],
                                   self.repository.progress(team_id), self.repository.team_attempts(team_id),
                                   self.repository.enrolled_users(team_id), self.repository.pending_count(team_id))

    def team_kpis(self, caller: Caller, request: TeamRequest) -> dict:
        self._require_view_people(caller, request.team_id)
        return {"team_id": request.team_id, **self._team_kpis(request.team_id)}

    def company_kpis(self, caller: Caller) -> dict:
        if self.registry.org_role(caller.actor.user_id) not in OVERSIGHT_ROLES:
            raise Forbidden("Company viewer role required")
        departments = [{"team_id": team_id, "name": name, **self._team_kpis(team_id)}
                       for team_id, name in self.registry.list_teams()]
        return {"departments": departments, **reporting.company_totals(departments)}

    async def curriculum_set(self, caller: Caller, request: CurriculumSet) -> JsonValue:
        self._require_manage(caller, request.team_id)
        spec = request.curriculum.model_dump()
        hashes: dict[int, str] = {}
        missing = []
        flat = [lesson for module in spec["modules"] for lesson in module["lessons"]]
        for index, lesson in enumerate(flat):
            records, absent = await self._lesson_records(caller, request.team_id, lesson)
            missing.extend(absent)
            hashes[index] = lesson_hash(lesson, records)
        if missing:
            raise DomainError("Team memory records not found: " + ", ".join(map(str, sorted(set(missing)))))
        now = self._now()
        revision = self.repository.replace_curriculum(request.team_id, spec, request.expected_revision,
                                                      caller.actor.user_id, hashes, now)
        self.repository.log({"team_id": request.team_id, "user_id": caller.actor.user_id, "at": now,
                             "event": "curriculum_updated", "subject_id": str(revision),
                             "summary": f"{caller.actor.display_name} published curriculum revision {revision} on {now[:10]}"})
        return await self.curriculum_get(caller, TeamRequest(team_id=request.team_id))

    async def curriculum_draft(self, caller: Caller, request: TeamRequest) -> JsonValue:
        self._require_manage(caller, request.team_id)
        records, after = [], 0
        while len(records) < drafting.DRAFT_MAX_RECORDS:
            page = await self.source.export(caller, request.team_id, after, EXPORT_PAGE)
            records.extend(page)
            if len(page) < EXPORT_PAGE:
                break
            after = page[-1]["id"]
        name = dict(self.registry.list_teams()).get(request.team_id, request.team_id)
        draft = drafting.deterministic_draft(name, records)
        current = self.repository.curriculum(request.team_id)
        result = {"team_id": request.team_id, "records_considered": len(records),
                  "current_revision": 0 if current is None else current["revision"],
                  "llm_used": False, "llm_error": None}
        if not draft["modules"]:
            return {**result, "draft": None, "message": "Team memory has no active records to build lessons from"}
        model = await asyncio.to_thread(self.llm_factory)
        if model is not None:
            samples = {r["id"]: r.get("content", "") for r in records}
            try:
                polished = await asyncio.to_thread(llm.polish_curriculum, model, draft, samples)
                draft = drafting.apply_polish(draft, polished)
                result["llm_used"] = True
            except (LLMFailure, ValueError) as exc:
                LOGGER.warning(json.dumps({"event": "onboarding_llm_polish_failed", "team_id": request.team_id,
                                           "error": str(exc)[:300]}))
                result["llm_error"] = str(exc)[:300]
        CurriculumSpec.model_validate(draft)
        return {**result, "draft": draft,
                "message": "Draft only. Review and edit, then save with onboarding_curriculum_set "
                           "(curriculum=draft, expected_revision=current_revision)."}

    async def quiz_set(self, caller: Caller, request: QuizSet) -> JsonValue:
        module = self._module(request.module_id)
        self._require_manage(caller, module["team_id"])
        lesson_ids = {lesson["id"] for lesson in module["lessons"]}
        questions = []
        for question in request.questions:
            if question.lesson_id is not None and question.lesson_id not in lesson_ids:
                raise DomainError(f"Lesson {question.lesson_id} is not part of this module")
            data = question.model_dump()
            data["id"] = question.id or "q_" + uuid.uuid4().hex[:10]
            questions.append(data)
        now = self._now()
        revision = self.repository.replace_quiz(module["id"], questions, request.expected_revision,
                                                caller.actor.user_id, now)
        self.repository.log({"team_id": module["team_id"], "user_id": caller.actor.user_id, "at": now,
                             "event": "quiz_updated", "subject_id": module["id"],
                             "summary": f"{caller.actor.display_name} updated the quiz of module '{module['title']}' on {now[:10]}"})
        return {"module_id": module["id"], "revision": revision, "questions": questions}

    async def quiz_draft(self, caller: Caller, request: ModuleRequest) -> JsonValue:
        module = self._module(request.module_id)
        self._require_manage(caller, module["team_id"])
        model = await asyncio.to_thread(self.llm_factory)
        if model is None:
            raise Unavailable("No LLM is configured on the server; author questions with onboarding_quiz_set")
        texts = {}
        for lesson in module["lessons"]:
            records, _ = await self._lesson_records(caller, module["team_id"], lesson)
            texts[lesson["id"]] = lesson_material(lesson, records)
        count = min(DRAFT_QUESTIONS_MAX, DRAFT_QUESTIONS_PER_LESSON * len(texts))
        try:
            draft = await asyncio.to_thread(llm.draft_quiz, model, module, texts, count)
        except LLMFailure as exc:
            raise Unavailable(str(exc)) from exc
        questions, evidence, rejected = drafting.validate_draft_questions(draft.questions, texts)
        return {"module_id": module["id"], "current_revision": 0 if module["quiz"] is None else module["quiz"]["revision"],
                "questions": questions, "evidence": evidence, "rejected": rejected,
                "message": "Draft only. Review, then save with onboarding_quiz_set "
                           "(questions=questions, expected_revision=current_revision)."}

    async def grade(self, caller: Caller, request: GradeRequest) -> JsonValue:
        attempt = self.repository.attempt(request.attempt_id)
        if attempt is None:
            raise Conflict("Attempt not found")
        self._require_manage(caller, attempt["team_id"])
        answer = next((a for a in attempt["answers"] if a["question_id"] == request.question_id), None)
        if answer is None or answer["question"]["type"] != "open":
            raise DomainError("Only open answers of this attempt can be graded")
        if request.score > answer["points"]:
            raise DomainError(f"Score cannot exceed {answer['points']:g} points")
        module = self._module(attempt["module_id"])
        name = {uid: n for uid, n, _ in self.registry.team_members(attempt["team_id"])}.get(
            attempt["user_id"], attempt["user_id"])
        now = self._now()

        def finalize(current: dict, answers: list[dict]):
            if any(a["status"] == "pending" for a in answers):
                return None
            score = sum(a["score"] for a in answers)
            passed = score >= current["pass_threshold"] * current["max_score"]
            event = "quiz_regraded" if current["status"] == "graded" else "quiz_graded"
            verdict = self._verdict(score, current["max_score"], passed)
            log = {"team_id": current["team_id"], "user_id": current["user_id"], "at": now, "event": event,
                   "subject_id": str(current["id"]),
                   "summary": f"{name}'s quiz of module '{module['title']}' was graded by "
                              f"{caller.actor.display_name} on {now[:10]}: {verdict}"}
            note = f"Onboarding in department {current['team_id']}: quiz of module '{module['title']}' graded on {now[:10]}: {verdict}."
            return {"status": "graded", "score": score, "passed": int(passed), "graded_at": now}, log, note

        self.repository.grade_answer(request.attempt_id, request.question_id, request.score, request.comment,
                                     caller.actor.user_id, now, finalize)
        return self._attempt_view(module, request.attempt_id)

    # Personal memory notes

    async def _deliver_notes(self, caller: Caller) -> None:
        for note in self.repository.pending_notes(caller.actor.user_id):
            request_id = uuid.uuid5(NOTE_NAMESPACE, f"{self.repository.instance_id}:{note['id']}")
            try:
                result = await self.source.save_personal(caller, note["content"], ["onboarding", note["team_id"]], request_id)
            except DomainError as exc:
                LOGGER.warning(json.dumps({"event": "onboarding_personal_note_deferred", "user_id": caller.actor.user_id,
                                           "note_id": note["id"], "code": exc.code}))
                return
            outcome = "saved" if result.get("saved", True) else "rejected_by_quality_gate"
            self.repository.mark_delivered(note["id"], outcome, self._now())
