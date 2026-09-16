"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from jarvis import agent, auto, config, llm, orchestrator, server  # noqa: E402
from jarvis.tools import media  # noqa: E402

_BUILD_SPEC = importlib.util.spec_from_file_location("jarvis_installer_build", ROOT / "install" / "build.py")
assert _BUILD_SPEC and _BUILD_SPEC.loader
installer_build = importlib.util.module_from_spec(_BUILD_SPEC)
_BUILD_SPEC.loader.exec_module(installer_build)

_GATEWAY_SPEC = importlib.util.spec_from_file_location(
    "jarvis_image_gateway", ROOT / "gateway" / "image_gateway.py")
assert _GATEWAY_SPEC and _GATEWAY_SPEC.loader
image_gateway = importlib.util.module_from_spec(_GATEWAY_SPEC)
_GATEWAY_SPEC.loader.exec_module(image_gateway)


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


class RoutingAndPlanCostTests(unittest.TestCase):
    def test_social_gate_beats_agent_mode_and_forced_tier(self) -> None:
        original_get = orchestrator.CONFIG.get

        def forced_smart(path: str, default=None):
            if path == "orchestrator.force_tier":
                return "smart"
            return original_get(path, default)

        with mock.patch.object(orchestrator.CONFIG, "get", side_effect=forced_smart):
            route = orchestrator.choose_tier(
                "Привет!", agent_mode=True, has_tools=True,
            )
        self.assertEqual(route["tier"], "nano")
        self.assertFalse(route["offer_tools"])

        stream = [
            {"type": "delta", "text": "Привет! Рад тебя видеть."},
            {"type": "done", "tool_calls": []},
        ]
        with mock.patch.object(agent.llm, "chat_stream", return_value=stream), \
             mock.patch.object(agent.tools, "schemas", return_value=[]):
            events = list(agent.Agent(agent_mode=True).run(
                [{"role": "user", "content": "Привет!"}], user_text="Привет!",
            ))
        route_event = next(event for event in events if event.get("type") == "route")
        self.assertEqual(route_event["tier"], "nano")
        self.assertFalse(route_event["verbose"])
        self.assertFalse(any(event.get("type") == "plan" for event in events))
        self.assertFalse(any(event.get("type") in ("thinking", "reply_ui", "question")
                             for event in events))
        self.assertEqual([event for event in events if event.get("type") == "done"][-1]["content"],
                         "Привет! Рад тебя видеть.")

    def test_program_build_routes_directly_to_low_effort_coder(self) -> None:
        build = orchestrator.choose_tier(
            "Напиши игру ШАХМАТЫ", agent_mode=True, has_tools=True,
        )
        explanation = orchestrator.choose_tier(
            "Объясни правила игры шахматы", agent_mode=True, has_tools=True,
        )
        self.assertEqual(build["tier"], "coder")
        self.assertEqual(explanation["tier"], "base")
        self.assertEqual(llm._REASONING_EFFORT["coder"], "low")

    def test_plan_is_local_and_has_no_preflight_model_call(self) -> None:
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.llm, "chat") as blocking_chat:
            plan = runner.make_plan("Напиши игру шахматы")
        blocking_chat.assert_not_called()
        self.assertEqual(len(plan), 3)
        self.assertIn("Реализовать", plan[1])
        self.assertIn("Проверить", plan[2])

    def test_plan_progress_uses_two_natural_model_turns_not_one_per_item(self) -> None:
        route = {
            "tier": "coder", "reason": "test build", "score": 0.1,
            "verbose": True, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "write_file", "parameters": {"type": "object"}},
        }]
        turns = iter((
            [{
                "type": "done",
                "tool_calls": [{
                    "id": "write-1", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "chess.html", "content": "ready"}),
                    },
                }],
            }],
            [
                {"type": "delta", "text": "Игра сохранена и проверена."},
                {"type": "done", "tool_calls": []},
            ],
        ))
        observed_plan_steps = []
        runner = agent.Agent(agent_mode=True, chat_id="plan-cost")

        def model_turn(*_args, **_kwargs):
            observed_plan_steps.append(runner.plan_at)
            return next(turns)

        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=model_turn) as streamed, \
             mock.patch.object(agent.llm, "chat") as blocking_chat, \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(runner.run(
                [{"role": "user", "content": "Напиши игру шахматы"}],
                user_text="Напиши игру шахматы",
            ))

        self.assertEqual(streamed.call_count, 2)
        blocking_chat.assert_not_called()
        self.assertEqual(observed_plan_steps, [1, 3],
                         "verification starts as the current third step, not a completed one")
        progress = [event["step"] for event in events if event.get("type") == "plan_step"]
        self.assertEqual(progress, [1, 2, 3])
        third = next(i for i, event in enumerate(events)
                     if event.get("type") == "plan_step" and event.get("step") == 3)
        answer = next(i for i, event in enumerate(events)
                      if event.get("type") == "delta" and "сохранена" in event.get("text", ""))
        self.assertLess(third, answer, "verification segment must become current before its model turn")


class AutomaticMemoryTests(unittest.TestCase):
    def test_city_and_food_facts_are_extracted_without_an_llm(self) -> None:
        text = "Я переехал из Москвы в Казань и люблю острую еду. У меня есть аллергия на арахис."
        facts = agent.extract_obvious_memories(text)
        keyed = {item["key"]: item["value"] for item in facts}
        self.assertEqual(keyed["Город"], "Казань")
        self.assertEqual(keyed["Питание: предпочтения"], "острую еду")
        self.assertEqual(keyed["Питание: аллергия"], "арахис")
        self.assertEqual(agent.extract_obvious_memories("Я переехал в новую квартиру."), [])

        stored = []
        with mock.patch.object(agent.db, "remember", side_effect=lambda *args: stored.append(args) or {"ok": True}), \
             mock.patch.object(agent.llm, "chat") as model:
            agent.remember_obvious_facts(text)
        model.assert_not_called()
        self.assertIn(("person", "Город", "Казань", 1.15), stored)
        self.assertIn(("preference", "Питание: предпочтения", "острую еду", 1.15), stored)
        self.assertIn(("preference", "Питание: аллергия", "арахис", 1.15), stored)


class DirectDelayedDeliveryTests(unittest.TestCase):
    def test_safe_one_shot_message_is_delivered_without_headless_agent(self) -> None:
        self.assertEqual(auto.safe_delayed_message(
            "Напиши через 2 секунды: привет", "in 2s"), "привет")
        self.assertEqual(auto.safe_delayed_message(
            "Через 2 секунды напомни выключить чайник", "in 2s"),
            "Напоминание: выключить чайник")
        self.assertIsNone(auto.safe_delayed_message(
            "Напиши через 2 секунды игру шахматы", "in 2s"))
        self.assertIsNone(auto.safe_delayed_message(
            "Напиши каждый день привет", "every 1d"))

        task = {
            "id": "timer-1", "chat_id": "chat-1", "title": "Приветствие",
            "prompt": "Напиши через 2 секунды: привет", "schedule": "in 2s",
        }
        with mock.patch.object(auto.db, "get_task", return_value=task), \
             mock.patch.object(auto.db, "update_task") as update, \
             mock.patch.object(auto.db, "append_task_event"), \
             mock.patch.object(auto.db, "add_message") as add_message, \
             mock.patch.object(auto.db, "notify") as notify, \
             mock.patch.object(auto.agent, "run_headless") as headless, \
             mock.patch.object(auto, "_telegram_report"):
            auto.execute_task("timer-1")

        headless.assert_not_called()
        notify.assert_not_called()
        update.assert_any_call("timer-1", status="done", progress=1.0, result="привет")
        add_message.assert_called_once_with(
            "chat-1", "assistant", "привет",
            {"task_id": "timer-1", "from_auto": True, "files": [], "title": "Приветствие"},
        )
        self.assertNotIn("timer-1", auto._RUNNING)


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
             mock.patch.object(server.agent, "remember_obvious_facts") as remember_facts, \
             mock.patch.object(server.agent, "build_system_prompt", return_value="BASE SYSTEM"), \
             mock.patch.object(server.agent.tools, "schemas", return_value=[]):
            server.Handler._chat_stream(handler, {
                "chat_id": "camera-chat",
                "kind": "cam",
                "text": "Сделай мем",
                "attachments": [attachment],
            })

        self.assertEqual(chat_stream.call_count, 1, "vision UI must not require a preflight model call")
        remember_facts.assert_called_once_with("Сделай мем")
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

    def test_plain_clarification_gets_deterministic_interactive_control(self) -> None:
        """Interactive questions do not depend on images or prompt obedience."""
        question = "Какой формат результата тебе нужен?"

        def fake_stream(*_args, **_kwargs):
            yield {"type": "delta", "text": question}
            yield {"type": "done", "tool_calls": [{
                "id": "premature-file",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "result.txt", "content": "guess"}),
                },
            }]}

        route = {
            "tier": "base", "reason": "clarification", "score": 0,
            "verbose": False, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "write_file", "parameters": {"type": "object"}},
        }]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream) as stream, \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(agent.Agent().run(
                [{"role": "user", "content": "Сделай документ"}],
                user_text="Сделай документ", has_image=False,
            ))

        stream.assert_called_once()
        dispatch.assert_not_called()
        done = [event for event in events if event.get("type") == "done"][-1]
        self.assertIn(question, done["content"])
        self.assertIn("```ui\ntext Твой ответ = Напиши свой вариант\n```", done["content"])
        self.assertTrue(agent.has_interactive_ui(done["content"]))
        direct_ui = [event for event in events if event.get("type") == "reply_ui"]
        self.assertEqual(direct_ui, [{
            "type": "reply_ui", "spec": "text Твой ответ = Напиши свой вариант",
        }], "SSE must carry a parser-independent frontend control specification")
        self.assertEqual(done["tools"], [])

    def test_clarification_fallback_preserves_listed_options_as_tiles(self) -> None:
        text = "Какой формат выбрать?\n- PDF\n- Word\n- Markdown"
        panel = agent.reply_ui_fallback(text)
        self.assertIn("tiles Какой формат выбрать: PDF | Word | Markdown", panel)
        self.assertTrue(agent.needs_reply_ui(text))
        self.assertTrue(agent.has_interactive_ui(panel))

        # Самый частый реальный формат ответа: сначала красивые пункты с
        # описаниями, вопрос «что выбираем?» — последней строкой. Раньше gate
        # видел вопрос, но fallback искал варианты только ПОСЛЕ него.
        before_question = (
            "Есть три варианта:\n"
            "1. **Минимализм** — чистый светлый кадр\n"
            "2. **Ретро** — плёнка и зерно\n"
            "3. **Кино** — контраст и широкий формат\n"
            "Какой вариант выбираем?"
        )
        before_panel = agent.reply_ui_fallback(before_question)
        self.assertTrue(agent.needs_reply_ui(before_question, "Сделай обложку"))
        self.assertIn("tiles Какой вариант выбираем: Минимализм | Ретро | Кино", before_panel)
        self.assertTrue(agent.has_interactive_ui(before_panel))

        # «Вот варианты» без знака вопроса — тоже ожидание решения, если сам
        # пользователь не просил выдать каталог альтернатив как конечный ответ.
        implicit = "Варианты:\n- Быстро\n- Точно\n- С балансом"
        self.assertTrue(agent.needs_reply_ui(implicit, "Выполни задачу"))
        self.assertFalse(agent.needs_reply_ui(implicit, "Предложи варианты выполнения"))

        unrelated = agent.reply_ui_fallback(
            "Могу подготовить:\n- отчёт\n- таблицу\nНо какой дедлайн?"
        )
        self.assertIn("text Твой ответ", unrelated)
        self.assertNotIn("tiles", unrelated)
        self.assertFalse(agent.needs_reply_ui(
            "Готовая сводка с фактами и источниками. Без встречного вопроса."
        ))
        self.assertFalse(agent.needs_reply_ui(
            "Пять вопросов для собеседования:\n"
            "1. Почему вы выбрали эту профессию?\n"
            "2. Каким достижением вы гордитесь?\n"
            "3. Как вы решаете конфликты?"
        ), "a delivered list of questions is content, not a clarification")

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


class InstallerBuildTests(unittest.TestCase):
    def test_installer_uses_the_application_version(self) -> None:
        self.assertEqual(installer_build.version(), "1.2.0-beta.1")

    def test_rebuild_preserves_previous_embedded_keys_without_a_keys_file(self) -> None:
        cloud, deep, gigachat = "cloud-fixture", "deep-fixture", "gigachat-fixture"
        gateway_url, gateway_token = "https://images.example.ru", "release-token"
        setup = ('"$PY" - "$HOME_DIR" "%s" "%s" "%s" "%s" "%s" <<\'PYSETUP\'\n' % (
            base64.b64encode(cloud.encode()).decode(),
            base64.b64encode(deep.encode()).decode(),
            base64.b64encode(gigachat.encode()).decode(),
            base64.b64encode(gateway_url.encode()).decode(),
            base64.b64encode(gateway_token.encode()).decode(),
        ))
        with tempfile.TemporaryDirectory() as td:
            old_out = Path(td) / "JARVIS.command"
            old_out.write_text("#!/bin/bash\n" + setup, encoding="utf-8")
            with mock.patch.object(installer_build, "OUT", old_out):
                self.assertEqual(
                    installer_build._keys_from_previous_installer(),
                    (cloud, deep, gigachat, gateway_url, gateway_token),
                )

    def test_legacy_two_key_installer_migrates_with_empty_image_key(self) -> None:
        cloud, deep = "old-cloud", "old-deep"
        setup = ('"$PY" - "$HOME_DIR" "%s" "%s" <<\'PYSETUP\'\n' % (
            base64.b64encode(cloud.encode()).decode(),
            base64.b64encode(deep.encode()).decode(),
        ))
        with tempfile.TemporaryDirectory() as td:
            old_out = Path(td) / "JARVIS.command"
            old_out.write_text("#!/bin/bash\n" + setup, encoding="utf-8")
            with mock.patch.object(installer_build, "OUT", old_out):
                self.assertEqual(
                    installer_build._keys_from_previous_installer(),
                    (cloud, deep, "", "", ""),
                )

    def test_payload_gzip_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_root = Path(td)
            (app_root / "jarvis").mkdir()
            (app_root / "jarvis" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
            with mock.patch.object(installer_build, "APP", app_root):
                first = installer_build.build_payload()
                second = installer_build.build_payload()
        self.assertEqual(first, second)
        self.assertEqual(first[:2], b"\x1f\x8b")


class ImageGatewayServiceTests(unittest.TestCase):
    def test_release_token_comparison_is_exact(self) -> None:
        choices = ("release-a", "release-b")
        self.assertEqual(image_gateway._constant_match("release-b", choices), "release-b")
        self.assertEqual(image_gateway._constant_match("release", choices), "")
        self.assertEqual(image_gateway._constant_match("", choices), "")

    def test_rate_gate_enforces_minute_and_daily_caps(self) -> None:
        gate = image_gateway.RateGate()
        with mock.patch.object(image_gateway, "PER_MINUTE", 2), \
             mock.patch.object(image_gateway, "PER_DAY", 5), \
             mock.patch.object(image_gateway, "GLOBAL_PER_DAY", 20):
            self.assertTrue(gate.allow("release:127.0.0.1"))
            self.assertTrue(gate.allow("release:127.0.0.1"))
            self.assertFalse(gate.allow("release:127.0.0.1"))

    def test_timeweb_key_stays_server_side_and_gateway_decodes_one_image(self) -> None:
        image = b"\x89PNG\r\n\x1a\n" + b"p" * 1400
        answer = json.dumps({
            "created": 1,
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        }).encode()
        with mock.patch.object(image_gateway, "TIMEWEB_KEY", "server-only-timeweb-key"), \
             mock.patch.object(image_gateway, "TIMEWEB_MODEL", "image-model"), \
             mock.patch.object(image_gateway, "_http", return_value=(answer, "application/json")) as call:
            actual, content_type, image_id = image_gateway._generate_timeweb(
                "blue city", 1600, 900)
        self.assertEqual(actual, image)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(image_id, hashlib.sha256(image).hexdigest()[:16])
        self.assertEqual(call.call_args.args[0],
                         "https://api.timeweb.ai/v1/images/generations")
        self.assertEqual(call.call_args.kwargs["headers"]["Authorization"],
                         "Bearer server-only-timeweb-key")
        payload = json.loads(call.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["size"], "1536x1024")

    def test_timeweb_accepts_only_trusted_https_asset_fallback(self) -> None:
        image = b"\xff\xd8\xff" + b"j" * 1400
        answer = json.dumps({
            "data": [{"url": "https://images.timeweb.cloud/generated/one.jpg"}],
        }).encode()
        with mock.patch.object(image_gateway, "_http", side_effect=[
            (answer, "application/json"), (image, "image/jpeg")]) as call:
            actual, content_type, _ = image_gateway._generate_timeweb("lake", 800, 1200)
        self.assertEqual(actual, image)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(json.loads(call.call_args_list[0].kwargs["data"])["size"],
                         "1024x1536")

        untrusted = json.dumps({
            "data": [{"url": "https://127.0.0.1/private-image"}],
        }).encode()
        with mock.patch.object(image_gateway, "_http",
                               return_value=(untrusted, "application/json")) as call:
            with self.assertRaises(image_gateway.GatewayError):
                image_gateway._generate_timeweb("lake", 1024, 1024)
        call.assert_called_once()

    def test_timeweb_rejects_malformed_and_oversized_base64_payloads(self) -> None:
        malformed = json.dumps({"data": [{"b64_json": "not base64 ***"}]}).encode()
        with mock.patch.object(image_gateway, "_http",
                               return_value=(malformed, "application/json")):
            with self.assertRaises(image_gateway.GatewayError):
                image_gateway._generate_timeweb("lake", 1024, 1024)

        image = b"\x89PNG\r\n\x1a\n" + b"p" * 1400
        oversized = json.dumps({
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
        }).encode()
        with mock.patch.object(image_gateway, "MAX_IMAGE_BYTES", 1200), \
             mock.patch.object(image_gateway, "_http",
                               return_value=(oversized, "application/json")):
            with self.assertRaises(image_gateway.GatewayError):
                image_gateway._generate_timeweb("lake", 1024, 1024)

    def test_gateway_dispatches_only_to_selected_upstream(self) -> None:
        sentinel = (b"image", "image/png", "id")
        with mock.patch.object(image_gateway, "UPSTREAM", "timeweb"), \
             mock.patch.object(image_gateway, "_generate_timeweb",
                               return_value=sentinel) as timeweb, \
             mock.patch.object(image_gateway, "_generate_gigachat") as gigachat:
            self.assertEqual(image_gateway.generate("p", 1, 2), sentinel)
        timeweb.assert_called_once_with("p", 1, 2)
        gigachat.assert_not_called()

    def test_provider_key_is_used_only_for_server_side_oauth(self) -> None:
        expires = int((time.time() + 1200) * 1000)
        oauth = json.dumps({"access_token": "short-lived", "expires_at": expires}).encode()
        with mock.patch.object(image_gateway, "AUTH_KEY", "server-only-b2b-key"), \
             mock.patch.object(image_gateway, "_ACCESS_TOKEN", ""), \
             mock.patch.object(image_gateway, "_ACCESS_EXPIRES", 0), \
             mock.patch.object(image_gateway, "_http", return_value=(oauth, "application/json")) as call:
            token = image_gateway._access_token()
        self.assertEqual(token, "short-lived")
        self.assertEqual(call.call_args.kwargs["headers"]["Authorization"],
                         "Basic server-only-b2b-key")
        self.assertNotIn("server-only-b2b-key", json.dumps({"token": token}))


class GigaChatImageTransportTests(unittest.TestCase):
    def test_bundled_russian_root_ca_has_the_published_fingerprint(self) -> None:
        ca_path = ROOT / "app" / "jarvis" / "certs" / "russian_trusted_root_ca.pem"
        der = ssl.PEM_cert_to_DER_cert(ca_path.read_text(encoding="ascii"))
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        self.assertEqual(
            fingerprint,
            "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31",
        )
        self.assertEqual(media._RUSSIAN_CA, ca_path)

    def setUp(self) -> None:
        with media._TOKEN_LOCK:
            media._TOKEN_CACHE.clear()

    def tearDown(self) -> None:
        with media._TOKEN_LOCK:
            media._TOKEN_CACHE.clear()

    @staticmethod
    def _config(path: str, default=None):
        values = {
            "media.gigachat_auth_key": "fixture-authorization-key",
            "media.gigachat_scope": "GIGACHAT_API_PERS",
            "media.image_provider": "gigachat",
            "media.gigachat_model": "GigaChat",
            "media.enhance_prompt": True,
        }
        return values.get(path, default)

    def test_oauth_token_is_cached_and_secret_is_only_sent_as_basic_auth(self) -> None:
        expires = int((media.time.time() + 1200) * 1000)
        answer = json.dumps({"access_token": "temporary-token", "expires_at": expires}).encode()
        with mock.patch.object(media.CONFIG, "get", side_effect=self._config), \
             mock.patch.object(media, "_http", return_value=(answer, "application/json")) as request:
            first = media._access_token()
            second = media._access_token()

        self.assertEqual(first, "temporary-token")
        self.assertEqual(second, first)
        request.assert_called_once()
        url = request.call_args.args[0]
        kwargs = request.call_args.kwargs
        self.assertEqual(url, media._GIGACHAT_OAUTH_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic fixture-authorization-key")
        self.assertEqual(kwargs["data"], b"scope=GIGACHAT_API_PERS")
        self.assertRegex(kwargs["headers"]["RqUID"], r"^[0-9a-f-]{36}$")

    def test_image_id_reads_message_not_unrelated_response_uuid(self) -> None:
        wanted = "123e4567-e89b-42d3-a456-426614174000"
        response = {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "choices": [{"message": {"content": '<img fuse="true" src="%s"/>' % wanted}}],
        }
        self.assertEqual(media._image_id(response), wanted)
        with self.assertRaises(media.GigaChatError):
            media._image_id({
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "choices": [{"message": {"content": "Изображение не создано"}}],
            })

    def test_generation_downloads_then_atomically_saves_one_watermark_free_image(self) -> None:
        file_id = "123e4567-e89b-42d3-a456-426614174000"
        image = b"\xff\xd8\xff" + b"x" * 1400
        answer = {"choices": [{"message": {"content": '<img src="%s"/>' % file_id}}]}
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "gigachat_image_123e4567.jpg"
            with mock.patch.object(media.CONFIG, "get", side_effect=self._config), \
                 mock.patch.object(media, "_enhance_prompt", return_value="точная сцена"), \
                 mock.patch.object(media, "_access_token", return_value="token") as token, \
                 mock.patch.object(media, "_json_request", return_value=answer) as generate, \
                 mock.patch.object(media, "_image_bytes", return_value=(image, ".jpg")) as download, \
                 mock.patch.object(media.sandbox, "safe_path", return_value=target), \
                 mock.patch.object(media.sandbox, "dl", return_value="/api/download/gigachat.jpg"):
                result = media.generate_image("город", width=1536, height=1024, style="cinematic")

            self.assertEqual(target.read_bytes(), image)
            self.assertFalse(target.with_suffix(".jpg.tmp").exists())

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "gigachat_image_123e4567.jpg")
        self.assertEqual(result["prompt"], "точная сцена")
        self.assertEqual(result["provider"], "gigachat")
        self.assertFalse(result["watermark"])
        token.assert_called_once_with()
        download.assert_called_once_with(file_id, "token")
        payload = generate.call_args.args[1]
        self.assertEqual(payload["model"], "GigaChat")
        self.assertEqual(payload["function_call"], "auto")
        self.assertIn("точная сцена", payload["messages"][-1]["content"])

    def test_gateway_sends_only_release_token_and_saves_returned_image(self) -> None:
        image = b"\xff\xd8\xff" + b"g" * 1400
        digest = hashlib.sha256(image).hexdigest()[:16]

        class Response:
            headers = {"Content-Length": str(len(image)), "Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return image

        values = {
            "media.image_provider": "gateway",
            "media.image_gateway_url": "https://images.example.ru",
            "media.image_gateway_token": "release-token-not-provider-key",
            "media.gigachat_auth_key": "",
            "media.enhance_prompt": True,
        }

        def get_config(path: str, default=None):
            return values.get(path, default)

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / ("jarvis_image_%s.jpg" % digest)
            with mock.patch.object(media.CONFIG, "get", side_effect=get_config), \
                 mock.patch.object(media, "_enhance_prompt", return_value="точная сцена"), \
                 mock.patch.object(media.urllib.request, "urlopen", return_value=Response()) as request, \
                 mock.patch.object(media.sandbox, "safe_path", return_value=target), \
                 mock.patch.object(media.sandbox, "dl", return_value="/api/download/image.jpg"):
                result = media.generate_image("город", width=1536, height=1024)
                saved_image = target.read_bytes()

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "gateway")
        self.assertEqual(result["path"], target.name)
        self.assertEqual(saved_image, image)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://images.example.ru/v1/images/generations")
        self.assertEqual(sent.get_header("Authorization"), "Bearer release-token-not-provider-key")
        payload = json.loads(sent.data.decode("utf-8"))
        self.assertEqual(payload["prompt"], "точная сцена")
        self.assertEqual(payload["width"], 1536)
        self.assertNotIn("gigachat", json.dumps(payload).lower())

    def test_config_migrates_legacy_providers_and_masks_authorization_key(self) -> None:
        self.assertEqual(config.DEFAULTS["media"]["image_provider"], "auto")
        migrated = config._migrate({"media": {
            "image_provider": "puter", "image_base": "legacy", "image_model": "sana",
            "puter": {"old": True},
        }})["media"]
        self.assertEqual(migrated["image_provider"], "auto")
        self.assertNotIn("image_base", migrated)
        self.assertNotIn("image_model", migrated)
        self.assertNotIn("puter", migrated)
        self.assertEqual(config._migrate({"media": {"image_provider": "off"}})
                         ["media"]["image_provider"], "off")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(config, "CONFIG_PATH", Path(td) / "config.json"):
            cfg = config.Config()
            cfg.set("media.gigachat_auth_key", "1234567890-secret")
            cfg.set("media.image_gateway_url", "https://images.example.ru")
            cfg.set("media.image_gateway_token", "never-return-this-token")
            public = cfg.public()["media"]
        self.assertTrue(public["has_gigachat_key"])
        self.assertNotEqual(public["gigachat_auth_key"], "1234567890-secret")
        self.assertIn("…", public["gigachat_auth_key"])
        self.assertTrue(public["has_image_gateway"])
        self.assertEqual(public["image_gateway_token"], "…")

    def test_non_generation_media_api_remains_registered(self) -> None:
        for name in ("transcribe_audio", "analyze_image", "analyze_video",
                     "send_telegram", "telegram_send_file"):
            self.assertTrue(callable(getattr(media, name, None)), name)
            self.assertIn(name, agent.tools.TOOLS)


class ImageGenerationContractTests(unittest.TestCase):
    ROUTE = {
        "tier": "base", "reason": "image test", "score": 0,
        "verbose": False, "offer_tools": True,
    }
    SCHEMA = [{
        "type": "function",
        "function": {"name": "generate_image", "parameters": {"type": "object"}},
    }]

    @staticmethod
    def _tool_turn(call_id: str, prompt: str = "blue glass city") -> list[dict]:
        return [{
            "type": "done",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "generate_image",
                    "arguments": json.dumps({"prompt": prompt, "width": 1024}),
                },
            }],
        }]

    def test_agent_uses_backend_image_and_emits_file_after_tool_result_exists(self) -> None:
        streams = iter((
            self._tool_turn("image-1"),
            [{"type": "delta", "text": "Изображение готово."},
             {"type": "done", "tool_calls": []}],
        ))
        saved = {
            "ok": True, "path": "gigachat.jpg", "name": "gigachat.jpg", "size": 2048,
            "download_url": "/api/download/gigachat.jpg",
            "preview_url": "/api/download/gigachat.jpg",
            "model": "GigaChat · text2image", "provider": "gigachat", "watermark": False,
        }
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=self.ROUTE), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(streams)), \
             mock.patch.object(agent.tools, "schemas", return_value=self.SCHEMA), \
             mock.patch.object(agent.tools, "call", return_value=saved) as dispatch:
            runner = agent.Agent(chat_id="image-chat")
            events = list(runner.run(
                [{"role": "user", "content": "Нарисуй город в синем стекле"}],
                user_text="Нарисуй город в синем стекле",
            ))

        dispatch.assert_called_once_with(
            "generate_image", {"prompt": "blue glass city", "width": 1024})
        self.assertFalse(any(event.get("type") == "image_request" for event in events),
                         "GigaChat generation must not depend on browser handoff")
        files = [event for event in events if event.get("type") == "file"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "gigachat.jpg")
        tool_result = next(event for event in events if event.get("type") == "tool_result")
        self.assertTrue(tool_result["result"]["ok"])
        self.assertLess(events.index(files[0]), events.index(tool_result))
        self.assertEqual(runner.created_files, [{
            "name": "gigachat.jpg", "url": "/api/download/gigachat.jpg",
            "size": 2048, "kind": "image",
        }])

    def test_repeated_exact_image_call_dispatches_and_emits_file_once(self) -> None:
        streams = iter((
            self._tool_turn("image-1"),
            self._tool_turn("image-2"),
            [{"type": "delta", "text": "Один результат готов."},
             {"type": "done", "tool_calls": []}],
        ))
        saved = {
            "ok": True, "path": "only.png", "size": 2048,
            "download_url": "/api/download/only.png",
        }
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=self.ROUTE), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(streams)), \
             mock.patch.object(agent.tools, "schemas", return_value=self.SCHEMA), \
             mock.patch.object(agent.tools, "call", return_value=saved) as dispatch:
            runner = agent.Agent(chat_id="dedupe-chat")
            events = list(runner.run(
                [{"role": "user", "content": "Нарисуй город"}],
                user_text="Нарисуй город",
            ))

        dispatch.assert_called_once_with("generate_image", {"prompt": "blue glass city", "width": 1024})
        self.assertEqual(runner.used_tools, ["generate_image"])
        self.assertEqual(len([event for event in events if event.get("type") == "file"]), 1)
        self.assertEqual(len(runner.created_files), 1)
        done = [event for event in events if event.get("type") == "done"][-1]
        self.assertEqual(len(done["files"]), 1)

    def test_long_prompts_with_the_same_first_300_characters_stay_distinct(self) -> None:
        prefix = "cinematic blue glass city, " * 14
        self.assertGreater(len(prefix), 300)
        first_prompt = prefix + "at sunrise"
        second_prompt = prefix + "at midnight"
        streams = iter((
            self._tool_turn("image-1", first_prompt),
            self._tool_turn("image-2", second_prompt),
            self._tool_turn("image-3", second_prompt),  # exact repeat remains idempotent
            [{"type": "delta", "text": "Два разных результата готовы."},
             {"type": "done", "tool_calls": []}],
        ))
        serial = iter(("sunrise.jpg", "midnight.jpg"))

        def save_image(_name: str, _args: dict) -> dict:
            name = next(serial)
            return {
                "ok": True, "path": name, "size": 2048,
                "download_url": "/api/download/" + name,
            }

        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=self.ROUTE), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(streams)), \
             mock.patch.object(agent.tools, "schemas", return_value=self.SCHEMA), \
             mock.patch.object(agent.tools, "call", side_effect=save_image) as dispatch:
            runner = agent.Agent(chat_id="long-prompts-chat")
            events = list(runner.run(
                [{"role": "user", "content": "Сделай две версии города"}],
                user_text="Сделай две версии города",
            ))

        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(dispatch.call_args_list, [
            mock.call("generate_image", {"prompt": first_prompt, "width": 1024}),
            mock.call("generate_image", {"prompt": second_prompt, "width": 1024}),
        ])
        self.assertEqual(
            [event["name"] for event in events if event.get("type") == "file"],
            ["sunrise.jpg", "midnight.jpg"],
        )
        self.assertEqual(len(runner.created_files), 2)


if __name__ == "__main__":
    unittest.main()
