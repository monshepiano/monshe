"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from jarvis import agent, auto  # noqa: E402


class BackgroundRoutingTests(unittest.TestCase):
    def test_only_explicit_background_intent_is_routed_to_auto(self) -> None:
        foreground = [
            "Напиши игру ШАХМАТЫ",
            "Напиши мне игру ШАХМАТЫ",
            "Собери большой отчёт по рынку и сохрани Excel",
            "Исследуй рынок " + "очень подробно " * 50,
            "Создай большой проект с кодом и файлами",
        ]
        background = [
            "Напомни через 5 минут выключить чайник",
            "Проверяй каждый день курс доллара",
            "Сделай это в фоне",
            "Сделай это позже",
            "Мониторь цену каждый час",
        ]
        for text in foreground:
            with self.subTest(text=text):
                self.assertFalse(auto.should_background(text)["background"])
        for text in background:
            with self.subTest(text=text):
                self.assertTrue(auto.should_background(text)["background"])

    def test_agent_never_receives_server_only_schedule_tool(self) -> None:
        for computer_use in (False, True):
            schemas = agent.tools.schemas(agent._tool_groups(computer_use))
            names = {(item.get("function") or {}).get("name") for item in schemas}
            self.assertNotIn("schedule_task", names)

    def test_reserved_schedule_name_cannot_create_a_task_directly(self) -> None:
        """The parser sentinel itself is not a second executable AUTO route."""
        with mock.patch.object(auto, "create_background_task") as create:
            result = agent.tools.call("schedule_task", {
                "title": "Шахматы",
                "prompt": "Напиши игру ШАХМАТЫ",
                "schedule": "",
            })
        create.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("сервер", result["error"].lower())

    def test_hallucinated_schedule_call_cannot_bypass_schema(self) -> None:
        """Even a structured call omitted from schemas must not be dispatched."""
        turns = iter(("call", "answer"))

        def fake_stream(*_args, **_kwargs):
            if next(turns) == "call":
                yield {
                    "type": "done",
                    "tool_calls": [{
                        "id": "forbidden",
                        "type": "function",
                        "function": {
                            "name": "schedule_task",
                            "arguments": json.dumps({
                                "title": "Шахматы",
                                "prompt": "Напиши игру ШАХМАТЫ",
                            }, ensure_ascii=False),
                        },
                    }],
                }
            else:
                yield {"type": "delta", "text": "Выполняю прямо в диалоге."}
                yield {"type": "done", "tool_calls": []}

        route = {
            "tier": "base", "reason": "test", "score": 0,
            "verbose": False, "offer_tools": True,
        }
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "call") as dispatch:
            runner = agent.Agent(agent_mode=False)
            events = list(runner.run(
                [{"role": "user", "content": "Напиши игру ШАХМАТЫ"}],
                user_text="Напиши игру ШАХМАТЫ",
            ))

        dispatch.assert_not_called()
        self.assertNotIn("schedule_task", runner.used_tools)
        done = [event for event in events if event.get("type") == "done"]
        self.assertTrue(done)
        self.assertIn("диалоге", done[-1]["content"])


if __name__ == "__main__":
    unittest.main()
