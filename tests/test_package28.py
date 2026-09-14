"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from jarvis import agent, auto, server  # noqa: E402


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


class VisionUiContractTests(unittest.TestCase):
    def test_contract_requires_contextual_tiles_without_duplicate_submit_ui(self) -> None:
        contract = agent.turn_ui_contract(has_image=True)
        self.assertIn("```ui", contract)
        self.assertRegex(contract, r"tiles\s+Стиль:")
        self.assertIn("Варианты не оформляй\nобычным Markdown-списком", contract)
        self.assertIn("Не добавляй button\n«Сгенерировать»", contract)
        self.assertIn("«Свой вариант»", contract)
        self.assertIn("Если стиль уже\nявно задан", contract)
        self.assertIn("НЕ показывай ui для\nфактического вопроса, сводки новостей", contract)

    def test_image_contract_and_payload_share_one_ordered_model_call(self) -> None:
        """The UI reminder is a message in the vision request, not a preflight LLM."""
        history = [
            {"role": "user", "content": "Предыдущий вопрос"},
            {"role": "assistant", "content": "Предыдущий ответ"},
            {"role": "user", "content": "Сделай мем"},
        ]
        handler = mock.Mock()
        handler._sse.return_value = True
        handler._sse_open.return_value = None
        handler._sse_close.return_value = None

        stream_events = [
            {"type": "delta", "text": "Выбери стиль.\n```ui\ntiles Стиль: сухой | кино | ретро\n```"},
            {"type": "done", "tool_calls": []},
        ]
        route = {
            "tier": "vision", "reason": "image", "score": 0,
            "verbose": False, "offer_tools": False,
        }
        attachment = {
            "name": "frame.jpg", "kind": "image",
            "data": "data:image/jpeg;base64,ZmFrZQ==", "download_url": "/frame.jpg",
        }

        with mock.patch.object(server.llm, "active_providers", return_value=["test"]), \
             mock.patch.object(server.llm, "chat_stream", return_value=stream_events) as chat_stream, \
             mock.patch.object(server.llm, "chat") as blocking_chat, \
             mock.patch.object(server.db, "add_message", return_value={"id": "message-1"}), \
             mock.patch.object(server.db, "get_messages", return_value=history), \
             mock.patch.object(server.db, "rename_chat"), \
             mock.patch.object(server.sandbox, "set_chat"), \
             mock.patch.object(server.auto, "should_background",
                               return_value={"background": False, "schedule": "", "reason": ""}), \
             mock.patch.object(server.orchestrator, "summarize_history", side_effect=lambda items: items), \
             mock.patch.object(server.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(server.agent, "build_system_prompt", return_value="BASE SYSTEM"), \
             mock.patch.object(server.agent.tools, "schemas", return_value=[]):
            server.Handler._chat_stream(handler, {
                "chat_id": "camera-chat",
                "kind": "cam",
                "text": "Сделай мем",
                "attachments": [attachment],
            })

        self.assertEqual(chat_stream.call_count, 1, "vision UI must not require a preflight model call")
        blocking_chat.assert_not_called()
        messages = chat_stream.call_args.args[0]
        self.assertEqual(messages[0], {"role": "system", "content": "BASE SYSTEM"})
        self.assertEqual(messages[-2], {
            "role": "system", "content": agent.turn_ui_contract(has_image=True),
        })
        self.assertEqual(messages[-1]["role"], "user")
        parts = messages[-1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Сделай мем"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(parts[1]["image_url"]["url"], attachment["data"])
        self.assertNotIn(agent.turn_ui_contract(has_image=True), [
            item.get("content") for item in messages[:-2] if item.get("role") == "system"
        ])

    def test_creative_frame_classifier_stops_only_underspecified_generation(self) -> None:
        self.assertTrue(agent.needs_creative_image_choice("Сделай мем", has_image=True))
        self.assertTrue(agent.needs_creative_image_choice(
            "Преврати это в постер", has_image=True,
        ))
        self.assertFalse(agent.needs_creative_image_choice(
            "Сделай мем про понедельник", has_image=True,
        ))
        self.assertFalse(agent.needs_creative_image_choice(
            "Сделай постер в стиле киберпанк", has_image=True,
        ))
        self.assertFalse(agent.needs_creative_image_choice(
            "Что видно на фотографии?", has_image=True,
        ))
        self.assertFalse(agent.needs_creative_image_choice("Сделай мем", has_image=False))

    def test_generate_image_cannot_run_before_contextual_choice(self) -> None:
        """The contract is an execution gate, not a best-effort prompt hint."""
        turns = iter(("forbidden-generate", "choice"))

        def fake_stream(*_args, **_kwargs):
            turn = next(turns)
            if turn == "forbidden-generate":
                yield {
                    "type": "done",
                    "tool_calls": [{
                        "id": "image-before-choice",
                        "type": "function",
                        "function": {
                            "name": "generate_image",
                            "arguments": json.dumps({"prompt": "guess"}),
                        },
                    }],
                }
                return
            text = (
                "В кадре два человека у кофемашины — выбери шутку.\n\n"
                "```ui\ntiles Стиль: спор за кофе | утро понедельника | офисный шпион\n```"
            )
            yield {"type": "delta", "text": text}
            yield {"type": "done", "tool_calls": []}

        route = {
            "tier": "vision", "reason": "image", "score": 0,
            "verbose": False, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "generate_image", "parameters": {"type": "object"}},
        }]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(agent.Agent().run(
                [{"role": "user", "content": "Сделай мем"}],
                user_text="Сделай мем", has_image=True, require_ui_choice=True,
            ))

        dispatch.assert_not_called()
        done = [event for event in events if event.get("type") == "done"][-1]
        self.assertTrue(agent.has_choice_ui(done["content"]))
        self.assertIn("кофемашины", done["content"])
        self.assertNotIn("generate_image", done["tools"])

    def test_choice_panel_and_generation_in_same_turn_still_cannot_dispatch(self) -> None:
        """Rendering options is not consent; only the user's next turn is consent."""
        panel = (
            "Выбери идею для кадра.\n\n"
            "```ui\ntiles Стиль: сухой юмор | киноафиша | ретро\n```"
        )

        def fake_stream(*_args, **_kwargs):
            yield {"type": "delta", "text": panel}
            yield {
                "type": "done",
                "tool_calls": [{
                    "id": "premature-image",
                    "type": "function",
                    "function": {
                        "name": "generate_image",
                        "arguments": json.dumps({"prompt": "too early"}),
                    },
                }],
            }

        route = {
            "tier": "vision", "reason": "image", "score": 0,
            "verbose": False, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "generate_image", "parameters": {"type": "object"}},
        }]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream) as stream, \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(agent.Agent().run(
                [{"role": "user", "content": "Сделай мем"}],
                user_text="Сделай мем", has_image=True, require_ui_choice=True,
            ))

        stream.assert_called_once()
        dispatch.assert_not_called()
        done = [event for event in events if event.get("type") == "done"][-1]
        self.assertEqual(done["content"], panel)
        self.assertEqual(done["tools"], [])

    def test_choice_fence_cannot_borrow_separator_from_later_text(self) -> None:
        self.assertFalse(agent.has_choice_ui("```ui\ntext Тема\n```\nобычный A | B"))
        self.assertTrue(agent.has_choice_ui("```ui\ntiles Тема: A | B\n```"))

    def test_streamed_text_is_the_only_canonical_final_answer(self) -> None:
        streamed = ["Шаг один завершён.\n\n", "Шаг два завершён.\n\n", "Полный итог."]
        self.assertEqual(
            server._canonical_response_content(streamed, "Только последний пункт."),
            "".join(streamed),
        )
        self.assertEqual(
            server._canonical_response_content([], "Непроточный ответ."),
            "Непроточный ответ.",
        )

    def test_direct_vision_endpoint_still_has_one_media_owner(self) -> None:
        handler = mock.Mock()
        expected = {"ok": True, "description": "кадр"}
        with mock.patch.object(server.media, "analyze_image", return_value=expected) as analyze, \
             mock.patch.object(server.llm, "chat") as chat, \
             mock.patch.object(server.llm, "chat_stream") as chat_stream:
            result = server.Handler._vision(handler, {
                "image": "data:image/jpeg;base64,ZmFrZQ==",
                "question": "Что видно?",
            })
        self.assertEqual(result, expected)
        analyze.assert_called_once_with("data:image/jpeg;base64,ZmFrZQ==", "Что видно?")
        chat.assert_not_called()
        chat_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
