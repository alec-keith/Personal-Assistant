"""
Roman-Elite v1.1: Onboarding state machine.

6-wave interview that learns the user's life across identity/goals, health,
relationships, work, finance, and rules of engagement. State is persisted in
the onboarding_state PostgreSQL table so it survives restarts and channel switches.

The onboarding flow is prompt-driven — it injects context into the system prompt
so Claude conducts the interview naturally in Roman's voice, rather than using a
rigid code-driven Q&A loop.
"""

import json
import logging
from pathlib import Path
from typing import Any

from src.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Load wave definitions from the framework schema
_SCHEMA_PATH = Path(__file__).parent.parent.parent / "Framework" / "onboarding_schema.json"


def _load_schema() -> dict:
    try:
        return json.loads(_SCHEMA_PATH.read_text())
    except Exception:
        logger.error("Failed to load onboarding schema from %s", _SCHEMA_PATH)
        return {"waves": []}


ONBOARDING_SCHEMA = _load_schema()
WAVES = ONBOARDING_SCHEMA.get("waves", [])
WAVE_MAP = {w["id"]: w for w in WAVES}

TRIGGER_PHRASES = [
    "roman onboard me", "interview me", "learn my life",
    "set up my system", "start onboarding", "start the interview",
    "onboard", "onboarding",
]


class OnboardingManager:
    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    async def is_active(self) -> bool:
        state = await self._memory.get_onboarding_state()
        return state["status"] == "in_progress"

    async def is_complete(self) -> bool:
        state = await self._memory.get_onboarding_state()
        return state["status"] == "completed"

    async def get_state(self) -> dict:
        return await self._memory.get_onboarding_state()

    async def start(self) -> dict:
        """Begin the onboarding interview. Returns the initial state."""
        if not WAVES:
            return {"status": "completed"}
        first_wave = WAVES[0]
        await self._memory.update_onboarding_state(
            status="in_progress",
            current_wave_id=first_wave["id"],
            question_index=0,
            completed_waves=[],
            answers={},
        )
        return await self.get_state()

    async def record_answer(
        self,
        question_id: str,
        answer: str,
        is_followup: bool = False,
    ) -> dict:
        """
        Save the user's answer and advance to the next question.
        Also writes the answer to the appropriate memory stores per the wave config.
        Returns updated state.
        """
        state = await self.get_state()
        if state["status"] != "in_progress":
            return state

        wave_id = state["current_wave_id"]
        wave = WAVE_MAP.get(wave_id)
        if not wave:
            return state

        # Record the answer
        answers = state["answers"]
        if is_followup:
            # Append followup to existing answer
            if question_id in answers:
                answers[question_id]["followup_answer"] = answer
                answers[question_id]["followup_asked"] = True
            else:
                answers[question_id] = {"answer": answer, "skipped": False, "followup_asked": True, "followup_answer": answer}
        else:
            answers[question_id] = {"answer": answer, "skipped": False, "followup_asked": False}

        # Write to memory stores specified by the wave
        write_stores = wave.get("write_to_memory", ["long_term"])
        for store in write_stores:
            await self._memory.save_memory(
                store=store,
                content=f"[onboarding:{wave_id}:{question_id}] {answer}",
                metadata={"source": "onboarding", "wave_id": wave_id, "question_id": question_id},
                tags=["onboarding", wave_id],
            )

        # Advance question index
        new_index = state["question_index"] + 1
        await self._memory.update_onboarding_state(
            answers=answers,
            question_index=new_index,
        )

        return await self.get_state()

    async def skip_question(self) -> dict:
        """Skip the current question and advance."""
        state = await self.get_state()
        if state["status"] != "in_progress":
            return state

        wave = WAVE_MAP.get(state["current_wave_id"])
        if not wave:
            return state

        question = self._get_current_question(state)
        if question:
            answers = state["answers"]
            answers[question["id"]] = {"answer": "", "skipped": True, "followup_asked": False}
            new_index = state["question_index"] + 1
            await self._memory.update_onboarding_state(
                answers=answers,
                question_index=new_index,
            )

        return await self.get_state()

    async def advance_to_next_wave(self) -> dict:
        """Move to the next wave, or complete if all waves done."""
        state = await self.get_state()
        if state["status"] != "in_progress":
            return state

        completed = state["completed_waves"]
        current_wave_id = state["current_wave_id"]
        if current_wave_id and current_wave_id not in completed:
            completed.append(current_wave_id)

        # Find next wave
        current_idx = next((i for i, w in enumerate(WAVES) if w["id"] == current_wave_id), -1)
        next_idx = current_idx + 1

        if next_idx >= len(WAVES):
            # All waves complete
            await self._memory.update_onboarding_state(
                status="completed",
                completed_waves=completed,
                question_index=0,
            )
            return await self.get_state()

        next_wave = WAVES[next_idx]
        await self._memory.update_onboarding_state(
            current_wave_id=next_wave["id"],
            question_index=0,
            completed_waves=completed,
        )
        return await self.get_state()

    def should_advance_wave(self, state: dict) -> bool:
        """Check if the current wave is complete (all questions answered/skipped)."""
        wave = WAVE_MAP.get(state.get("current_wave_id", ""))
        if not wave:
            return True
        questions = wave.get("questions", [])
        return state["question_index"] >= len(questions)

    def _get_current_question(self, state: dict) -> dict | None:
        """Get the current question definition, or None if wave is done."""
        wave = WAVE_MAP.get(state.get("current_wave_id", ""))
        if not wave:
            return None
        questions = wave.get("questions", [])
        idx = state.get("question_index", 0)
        if idx >= len(questions):
            return None
        return questions[idx]

    def get_total_progress(self, state: dict) -> tuple[int, int]:
        """Returns (answered_count, total_questions)."""
        total = sum(len(w.get("questions", [])) for w in WAVES)
        answered = len(state.get("answers", {}))
        return answered, total

    def build_prompt_section(self, state: dict) -> str:
        """
        Build the onboarding context section for injection into the system prompt.
        This tells Claude where we are in the interview and what to ask next.
        """
        if state["status"] != "in_progress":
            return ""

        wave = WAVE_MAP.get(state.get("current_wave_id", ""))
        if not wave:
            return ""

        questions = wave.get("questions", [])
        current_q = self._get_current_question(state)
        answered, total = self.get_total_progress(state)
        wave_idx = next((i for i, w in enumerate(WAVES) if w["id"] == wave["id"]), 0) + 1

        # Build answered context for this wave
        wave_answers = []
        for q in questions[:state["question_index"]]:
            q_id = q["id"]
            ans = state["answers"].get(q_id, {})
            if ans.get("skipped"):
                wave_answers.append(f"- {q.get('prompt', q_id)}: [skipped]")
            elif ans.get("answer"):
                wave_answers.append(f"- {q.get('prompt', q_id)}: {ans['answer']}")

        answered_section = ""
        if wave_answers:
            answered_section = "\n\nAlready answered in this wave:\n" + "\n".join(wave_answers)

        current_prompt = current_q["prompt"] if current_q else "Wave complete — advance to next."
        q_idx = state["question_index"] + 1
        q_total = len(questions)

        return f"""## Active Onboarding Session
You are conducting an onboarding interview to learn about every area of Alec's life.

Current wave: {wave.get('title', wave['id'])} (wave {wave_idx} of {len(WAVES)})
Current question: "{current_prompt}" (question {q_idx} of {q_total})
Completed waves: {', '.join(state['completed_waves']) or 'None yet'}
Progress: {answered} of {total} total questions answered
{answered_section}

RULES:
- Ask the current question naturally — don't read it verbatim if it sounds robotic
- You may ask ONE follow-up per question if the answer is vague or interesting
- After the user answers, call `onboarding_save_answer` with the question_id and a concise summary
- If the user says "skip", call `onboarding_advance` to move to the next question
- If the user seems overwhelmed or tired, offer to pause and resume later
- If the user asks something unrelated, handle it normally but gently steer back
- When all questions in a wave are done, call `onboarding_advance` with skip_wave=true to move to the next wave
- Stay in Roman's voice — this should feel like a real conversation, not a form
- The current question_id is: {current_q['id'] if current_q else 'wave_done'}"""

    @staticmethod
    def is_trigger(text: str) -> bool:
        """Check if the user's message is an onboarding trigger phrase."""
        lower = text.lower().strip()
        return any(trigger in lower for trigger in TRIGGER_PHRASES)
