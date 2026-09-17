"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sqlite3
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from jarvis import agent, auto, config, db, ideas, llm, orchestrator, server, telemetry  # noqa: E402
from jarvis.tools import media, web  # noqa: E402

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

    def test_plan_is_semantic_and_has_no_generic_error_fallback(self) -> None:
        runner = agent.Agent(agent_mode=True)
        semantic = [
            "Определить правила ходов и состояния шахматной партии",
            "Реализовать доску, фигуры и проверку допустимых ходов",
            "Добавить шах, мат, пат и смену активного игрока",
            "Проверить рокировку, превращение пешки и завершение партии",
        ]
        with mock.patch.object(agent.llm, "chat", return_value={
            "content": json.dumps(semantic, ensure_ascii=False),
        }) as planner:
            plan = runner.make_plan("Напиши игру шахматы", ["write_file"])
        self.assertEqual(plan, semantic)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(planner.call_args.kwargs["tier"], "nano")
        self.assertIn("write_file", planner.call_args.args[0][-1]["content"])

        with mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("offline")):
            self.assertEqual(runner.make_plan("Напиши игру шахматы"), [],
                             "a planner error must not resurrect the old generic three-step card")

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

        semantic_plan = [
            "Создать шахматную доску и начальную расстановку",
            "Реализовать допустимые ходы фигур",
            "Проверить сохранённую игру и правила завершения",
        ]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=model_turn) as streamed, \
             mock.patch.object(agent.llm, "chat", return_value={
                 "content": json.dumps(semantic_plan, ensure_ascii=False),
             }) as planner, \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(runner.run(
                [{"role": "user", "content": "Напиши игру шахматы"}],
                user_text="Напиши игру шахматы",
            ))

        self.assertEqual(streamed.call_count, 2)
        planner.assert_called_once()
        self.assertEqual(observed_plan_steps, [0, 3],
                         "plan stays hidden until the first turn proves autonomous execution")
        progress = [event["step"] for event in events if event.get("type") == "plan_step"]
        self.assertEqual(progress, [1, 2, 3])
        third = next(i for i, event in enumerate(events)
                     if event.get("type") == "plan_step" and event.get("step") == 3)
        answer = next(i for i, event in enumerate(events)
                      if event.get("type") == "delta" and "сохранена" in event.get("text", ""))
        self.assertLess(third, answer, "verification segment must become current before its model turn")

    def test_normal_chat_never_emits_a_plan(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.4,
            "verbose": True, "offer_tools": False,
        }
        stream = [
            {"type": "delta", "text": "Обычный ответ."},
            {"type": "done", "tool_calls": []},
        ]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", return_value=stream), \
             mock.patch.object(agent.tools, "schemas", return_value=[]):
            events = list(agent.Agent(agent_mode=False).run(
                [{"role": "user", "content": "Разбери вопрос"}],
                user_text="Разбери вопрос",
            ))
        self.assertFalse(any(event.get("type") in ("plan", "plan_step") for event in events))

    def test_agent_clarification_finishes_without_ever_showing_a_plan(self) -> None:
        route = {
            "tier": "base", "reason": "missing format", "score": 0.5,
            "verbose": True, "offer_tools": False,
        }
        stream = [
            {"type": "delta", "text": "Какой формат тебе нужен?\n- PDF\n- Word\n- Markdown"},
            {"type": "done", "tool_calls": []},
        ]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", return_value=stream), \
             mock.patch.object(agent.llm, "chat") as planner, \
             mock.patch.object(agent.tools, "schemas", return_value=[]):
            events = list(agent.Agent(agent_mode=True).run(
                [{"role": "user", "content": "Сделай документ"}],
                user_text="Сделай документ",
            ))
        planner.assert_not_called()
        self.assertFalse(any(event.get("type") in ("plan", "plan_step") for event in events))
        self.assertTrue(any(event.get("type") == "reply_ui" for event in events))
        first_delta = next(event for event in events if event.get("type") == "delta")
        self.assertIn("Какой формат", first_delta["text"])

    def test_unauthorized_tool_alone_does_not_prove_autonomous_execution(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.5,
            "verbose": True, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "write_file", "parameters": {"type": "object"}},
        }]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "unknown-1", "type": "function",
                "function": {"name": "nonexistent_tool", "arguments": "{}"},
            }]}],
            [{"type": "delta", "text": "Не могу выполнить это действие."},
             {"type": "done", "tool_calls": []}],
        ))
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.llm, "chat") as planner, \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(agent.Agent(agent_mode=True).run(
                [{"role": "user", "content": "Сделай задачу"}],
                user_text="Сделай задачу",
            ))
        planner.assert_not_called()
        dispatch.assert_not_called()
        self.assertFalse(any(event.get("type") == "plan" for event in events))

    def test_agent_uses_one_preflight_panel_and_never_receives_ask_user(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.5,
            "verbose": True, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        } for name in ("ask_user", "write_file")]
        stream = [
            {"type": "delta", "text": (
                "Уточню всё необходимое сразу.\n\n```ui\n"
                "tiles Формат: PDF | Word\n"
                "slider Объём 1..10 = 4\n```")},
            {"type": "done", "tool_calls": []},
        ]
        runner = agent.Agent(agent_mode=True)
        captured = {}

        def chat_stream(*_args, **kwargs):
            captured["tools"] = kwargs.get("tools", [])
            return stream

        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=chat_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "make_plan") as planner:
            events = list(runner.run(
                [{"role": "user", "content": "Сделай документ"}],
                user_text="Сделай документ",
            ))
        names = [(item.get("function") or {}).get("name") for item in captured["tools"]]
        self.assertEqual(names, ["write_file"])
        panels = [event for event in events if event.get("type") == "reply_ui"]
        self.assertEqual(len(panels), 1)
        self.assertIn("tiles Формат", panels[0]["spec"])
        self.assertIn("slider Объём", panels[0]["spec"])
        self.assertFalse(any(event.get("type") == "plan" for event in events))
        planner.assert_not_called()

    def test_separate_model_fences_are_merged_into_one_preflight_panel(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.5,
            "verbose": True, "offer_tools": True,
        }
        stream = [
            {"type": "delta", "text": (
                "Уточню параметры.\n\n```ui\ntiles Формат: PDF | Markdown\n```\n"
                "И тон.\n\n```ui\ntiles Тон: Деловой | Дружелюбный\n```"
            )},
            {"type": "done", "tool_calls": []},
        ]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", return_value=stream), \
             mock.patch.object(agent.tools, "schemas", return_value=[]):
            events = list(agent.Agent(agent_mode=True).run(
                [{"role": "user", "content": "Сделай документ"}],
                user_text="Сделай документ",
            ))
        panels = [event for event in events if event.get("type") == "reply_ui"]
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["spec"],
                         "tiles Формат: PDF | Markdown\ntiles Тон: Деловой | Дружелюбный")

    def test_answered_agent_preflight_cannot_open_a_second_panel(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.5,
            "verbose": True, "offer_tools": True,
        }
        schema = [{"type": "function", "function": {
            "name": "write_file", "parameters": {"type": "object"},
        }}]
        turns = iter((
            [
                {"type": "delta", "text": "Ещё один вопрос?\n\n```ui\ntiles Тон: Деловой | Дружелюбный\n```"},
                {"type": "done", "tool_calls": [{
                    "id": "premature-write", "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({
                        "path": "wrong.md", "content": "Нельзя выполнять вместе с вопросом",
                    }, ensure_ascii=False)},
                }]},
            ],
            [{"type": "done", "tool_calls": [{
                "id": "write-1", "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({
                    "path": "document.md", "content": "Готово",
                }, ensure_ascii=False)},
            }]}],
            [
                {"type": "delta", "text": "Документ готов."},
                {"type": "done", "tool_calls": []},
            ],
        ))
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}) as dispatch, \
             mock.patch.object(runner, "make_plan", return_value=[
                 "Собрать содержимое", "Записать документ", "Проверить файл",
             ]) as planner:
            events = list(runner.run(
                [{"role": "user", "content": "Формат: Markdown"}],
                user_text="Формат: Markdown", preflight_resolved=True,
            ))
        self.assertFalse(any(event.get("type") == "reply_ui" for event in events))
        shown = "".join(event.get("text", "") for event in events if event.get("type") == "delta")
        self.assertNotIn("Ещё один вопрос", shown)
        self.assertIn("Документ готов", shown)
        planner.assert_called_once_with("Формат: Markdown", ["write_file"])
        dispatch.assert_called_once_with(
            "write_file", {"path": "document.md", "content": "Готово"},
        )

    def test_tiny_reasoning_is_hidden_but_substantial_reasoning_is_streamed(self) -> None:
        route = {
            "tier": "smart", "reason": "test", "score": 0.8,
            "verbose": True, "offer_tools": False,
        }

        def run_with(reasoning_parts):
            stream = ([{"type": "reasoning", "text": part} for part in reasoning_parts] + [
                {"type": "delta", "text": "Готовый ответ."},
                {"type": "done", "tool_calls": []},
            ])
            with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
                 mock.patch.object(agent.llm, "chat_stream", return_value=stream), \
                 mock.patch.object(agent.tools, "schemas", return_value=[]):
                return list(agent.Agent(agent_mode=True).run(
                    [{"role": "user", "content": "Реши сложную задачу"}],
                    user_text="Реши сложную задачу",
                ))

        tiny = run_with(["Проверяю."])
        self.assertFalse(any(event.get("type") == "thinking" for event in tiny))
        parts = ["Сначала проверяю исходные ограничения и зависимости. ",
                 "Затем сопоставляю варианты, риски и проверяемый итог решения."]
        substantial = run_with(parts)
        thinking = [event["text"] for event in substantial if event.get("type") == "thinking"]
        self.assertEqual(thinking, ["".join(parts)])


class TerminalPermissionTests(unittest.TestCase):
    def test_visible_terminal_opening_is_always_a_soft_permission(self) -> None:
        self.assertTrue(agent.opens_terminal("open_app", {"name": "Terminal"}))
        self.assertTrue(agent.opens_terminal("open_app", {"name": "Терминал"}))
        self.assertTrue(agent.opens_terminal("run_shell", {"command": "open -a Terminal"}))
        self.assertTrue(agent.opens_terminal("run_python", {
            "code": "import os; os.system('open -a Terminal')",
        }))
        self.assertFalse(agent.opens_terminal("open_app", {"name": "Safari"}))
        self.assertEqual(agent.needs_approval("open_app", {"name": "Terminal"}),
                         "открытие приложения «Терминал»")
        self.assertEqual(agent.approval_style("open_app", {"name": "Terminal"}),
                         "permission")
        self.assertEqual(agent.approval_style("run_shell", {"command": "open -a Terminal"}),
                         "danger", "a shell command keeps its stronger existing sanction")

    def test_terminal_permission_is_emitted_before_dispatch(self) -> None:
        route = {
            "tier": "base", "reason": "test", "score": 0.4,
            "verbose": True, "offer_tools": True,
        }
        schema = [{
            "type": "function",
            "function": {"name": "open_app", "parameters": {"type": "object"}},
        }]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "open-terminal", "type": "function",
                "function": {"name": "open_app", "arguments": json.dumps({"name": "Terminal"})},
            }]}],
            [
                {"type": "delta", "text": "Терминал не открыт."},
                {"type": "done", "tool_calls": []},
            ],
        ))
        runner = agent.Agent(agent_mode=False)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "_wait_approval", return_value={"status": "rejected"}) as wait, \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(runner.run(
                [{"role": "user", "content": "Открой Terminal"}],
                user_text="Открой Terminal",
            ))

        approval_at = next(i for i, event in enumerate(events)
                           if event.get("type") == "approval_wait")
        done_at = next(i for i, event in enumerate(events)
                       if event.get("type") == "approval_done")
        self.assertLess(approval_at, done_at)
        self.assertEqual(events[approval_at]["style"], "permission")
        self.assertEqual(events[approval_at]["reason"], "открытие приложения «Терминал»")
        wait.assert_called_once_with(
            "open_app", {"name": "Terminal"}, "открытие приложения «Терминал»",
            style="permission",
        )
        dispatch.assert_not_called()


class AutomaticMemoryTests(unittest.TestCase):
    def test_general_preferences_are_extracted_without_topic_vocabularies(self) -> None:
        text = "Я люблю острую еду, мне нравятся прогулки, но я не переношу арахис."
        facts = agent.extract_obvious_memories(text)
        triples = {(item["key"], item["value"]) for item in facts}
        self.assertIn(("Предпочтение: острую еду", "острую еду"), triples)
        self.assertIn(("Предпочтение: прогулки", "прогулки"), triples)
        self.assertIn(("Ограничение: арахис", "арахис"), triples)
        self.assertEqual(agent.extract_obvious_memories("Я переехал в новую квартиру."), [])

        stored = []
        with mock.patch.object(agent.db, "remember", side_effect=lambda *args: stored.append(args) or {"ok": True}), \
             mock.patch.object(agent.llm, "chat") as model:
            agent.remember_obvious_facts(text)
        model.assert_not_called()
        self.assertIn(("preference", "Предпочтение: острую еду", "острую еду", 1.15), stored)
        self.assertIn(("preference", "Ограничение: арахис", "арахис", 1.15), stored)

    def test_preference_values_remain_verbatim_without_special_examples(self) -> None:
        moved = agent.extract_obvious_memories("Я переехал в питер")
        joke = agent.extract_obvious_memories("Я сказал что пошутил и я всё же до сих пор в мск")
        steak = agent.extract_obvious_memories("Я люблю стейки")
        disliked = agent.extract_obvious_memories("Я не люблю стейки")
        self.assertEqual(moved, [])
        self.assertEqual(joke, [])
        self.assertEqual(steak, [{
            "kind": "preference", "key": "Предпочтение: стейки", "value": "стейки",
        }])
        self.assertEqual(disliked, [{
            "kind": "preference", "key": "Ограничение: стейки", "value": "стейки",
        }], "negative preference must not also be recorded as positive")

    def test_local_memory_covers_durable_grammar_without_auxiliary_llm(self) -> None:
        text = ("Меня зовут Марина, я работаю UX-дизайнером и использую "
                "MacBook Air M2 с 8 ГБ памяти.")
        stored = []
        with mock.patch.object(agent.llm, "chat") as semantic, \
             mock.patch.object(agent.db, "remember",
                               side_effect=lambda *args: stored.append(args) or {"ok": True}):
            saved = agent.remember_semantic_facts(text)

        self.assertEqual(len(saved), 3)
        self.assertEqual(stored, [
            ("person", "Имя / обращение", "Марина", 1.15),
            ("person", "Работа", "UX-дизайнером", 1.15),
            ("fact", "Основной инструмент", "MacBook Air M2 с 8 ГБ памяти", 1.15),
        ])
        semantic.assert_not_called()

    def test_local_memory_splits_linked_first_person_clauses_without_duplicates(self) -> None:
        text = "Я люблю стейки и работаю редактором."
        with mock.patch.object(agent.llm, "chat") as semantic, \
             mock.patch.object(agent.db, "remember", return_value={"ok": True}) as writer:
            agent.remember_semantic_facts(text)
        self.assertEqual(writer.call_args_list, [
            mock.call("preference", "Предпочтение: стейки", "стейки", 1.15),
            mock.call("person", "Работа", "редактором", 1.15),
        ])
        semantic.assert_not_called()

    def test_local_memory_has_generic_explicit_escape_hatch_and_secret_gate(self) -> None:
        explicit = agent.extract_obvious_memories("Учти, у меня двое детей.")
        self.assertEqual(explicit, [{
            "kind": "fact", "key": "Явный факт: у меня двое детей",
            "value": "у меня двое детей",
        }])
        with mock.patch.object(agent.llm, "chat") as semantic:
            self.assertEqual(agent.remember_semantic_facts("Объясни квантовую физику"), [])
            self.assertEqual(agent.remember_semantic_facts("Мой API token — abc123"), [])
        semantic.assert_not_called()

    def test_memory_writer_merges_key_aliases_without_rewriting_values(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        try:
            with mock.patch.object(db, "_CONN", conn):
                first = db.remember("person", "Город", "Санкт-Петербург", 1.15)
                duplicate = db.remember("fact", "city", "питер")
                rows = db.recall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(duplicate["id"], first["id"])
                self.assertEqual((rows[0]["kind"], rows[0]["key"], rows[0]["value"]),
                                 ("person", "Город", "питер"))

                corrected = db.remember("fact", "location", "мск", 1.15)
                rows = db.recall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(corrected["id"], first["id"])
                self.assertEqual(rows[0]["value"], "мск")

                # Реальная upgrade-ситуация: старая база уже содержит обе
                # карточки, созданные предыдущей версией.
                conn.execute("DELETE FROM memory")
                conn.execute(
                    "INSERT INTO memory VALUES(?,?,?,?,?,?,?)",
                    ("old-ru", "person", "Город", "Санкт-Петербург", 1, 1, 1),
                )
                conn.execute(
                    "INSERT INTO memory VALUES(?,?,?,?,?,?,?)",
                    ("old-en", "fact", "city", "питер", 1, 2, 2),
                )
                conn.commit()
                upgraded = db.recall()
                self.assertEqual(len(upgraded), 1)
                self.assertEqual((upgraded[0]["kind"], upgraded[0]["key"], upgraded[0]["value"]),
                                 ("person", "Город", "питер"))

                conn.execute("DELETE FROM memory")
                job = db.remember("person", "Профессия", "UX-дизайнером")
                same_job = db.remember("fact", "occupation", "арт-директором")
                rows = db.recall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(same_job["id"], job["id"])
                self.assertEqual((rows[0]["kind"], rows[0]["key"], rows[0]["value"]),
                                 ("person", "Работа", "арт-директором"))
        finally:
            conn.close()


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
            "status": "queued",
        }
        running = {**task, "status": "running"}
        with mock.patch.object(auto.db, "get_task", side_effect=[task, running]), \
             mock.patch.object(auto.db, "update_task") as update, \
             mock.patch.object(auto.db, "append_task_event"), \
             mock.patch.object(auto.db, "add_message") as add_message, \
             mock.patch.object(auto.db, "notify") as notify, \
             mock.patch.object(auto.agent, "run_headless") as headless, \
             mock.patch.object(auto, "_telegram_report"):
            auto.execute_task("timer-1")

        headless.assert_not_called()
        notify.assert_not_called()
        update.assert_any_call("timer-1", status="done", resume_status="", progress=1.0, result="привет")
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
        self.assertIn("Не добавляй button «Сгенерировать»", contract)
        self.assertIn("Никогда не создавай\n`text`/`area`", contract)
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
            {"type": "model", "model": "vision-test-model"},
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
             mock.patch.object(server.db, "add_message", return_value={"id": "message-1"}) as add_message, \
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
        assistant_saves = [call for call in add_message.call_args_list
                           if len(call.args) >= 3 and call.args[1] == "assistant"]
        self.assertEqual(len(assistant_saves), 1)
        self.assertEqual(assistant_saves[0].args[3]["tier"], "vision")
        self.assertEqual(assistant_saves[0].args[3]["model"], "vision-test-model")
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
        self.assertEqual(done["content"], question)
        self.assertNotIn("```ui", done["content"])
        self.assertFalse(agent.has_interactive_ui(done["content"]))
        self.assertFalse([event for event in events if event.get("type") == "reply_ui"],
                         "plain clarification must use the main composer")
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
        self.assertEqual(unrelated, "", "deadline question uses the existing composer")
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
        self.assertEqual(installer_build.version(), "1.2.0-beta.5")

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


class LatencyAndResilienceTests(unittest.TestCase):
    class _Response:
        def __init__(self, lines=(), error=None):
            self.lines = list(lines)
            self.error = error

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield from self.lines
            if self.error:
                raise self.error

    @staticmethod
    def _span():
        span = mock.Mock()
        span.fields = {}
        return span

    def test_stream_falls_back_when_socket_dies_before_real_output(self) -> None:
        broken = self._Response(error=OSError("read failed before delta"))
        chunk = json.dumps({"choices": [{"delta": {"content": "готово"}}]}).encode()
        healthy = self._Response([b"data: " + chunk + b"\n", b"data: [DONE]\n"])
        with mock.patch.object(llm, "active_providers", return_value=["first", "second"]), \
             mock.patch.object(llm, "provider_conf", return_value={
                 "api_key": "test", "base_url": "https://provider.invalid",
             }), mock.patch.object(llm, "pick_model", side_effect=lambda _tier, provider: provider + "-model"), \
             mock.patch.object(llm, "_request", side_effect=[broken, healthy]) as request:
            events = list(llm._chat_stream_impl(
                [{"role": "user", "content": "test"}], _span=self._span(),
            ))
        self.assertEqual(request.call_count, 2)
        self.assertEqual([event.get("text") for event in events if event["type"] == "delta"],
                         ["готово"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["provider"], "second")

    def test_stream_never_falls_back_after_first_real_delta(self) -> None:
        chunk = json.dumps({"choices": [{"delta": {"content": "часть"}}]}).encode()
        broken = self._Response([b"data: " + chunk + b"\n"], OSError("late read failure"))
        with mock.patch.object(llm, "active_providers", return_value=["first", "second"]), \
             mock.patch.object(llm, "provider_conf", return_value={
                 "api_key": "test", "base_url": "https://provider.invalid",
             }), mock.patch.object(llm, "pick_model", side_effect=lambda _tier, provider: provider + "-model"), \
             mock.patch.object(llm, "_request", return_value=broken) as request:
            events = list(llm._chat_stream_impl(
                [{"role": "user", "content": "test"}], _span=self._span(),
            ))
        request.assert_called_once()
        self.assertEqual([event["type"] for event in events], ["model", "delta", "error"])
        self.assertIn("прерван", events[-1]["error"])

    def test_stream_clean_eof_before_output_falls_back(self) -> None:
        empty_eof = self._Response([])
        chunk = json.dumps({"choices": [{"delta": {"content": "резерв"}}]}).encode()
        healthy = self._Response([b"data: " + chunk + b"\n", b"data: [DONE]\n"])
        with mock.patch.object(llm, "active_providers", return_value=["first", "second"]), \
             mock.patch.object(llm, "provider_conf", return_value={
                 "api_key": "test", "base_url": "https://provider.invalid",
             }), mock.patch.object(llm, "pick_model", side_effect=lambda _tier, provider: provider + "-model"), \
             mock.patch.object(llm, "_request", side_effect=[empty_eof, healthy]) as request:
            events = list(llm._chat_stream_impl(
                [{"role": "user", "content": "test"}], _span=self._span(),
            ))
        self.assertEqual(request.call_count, 2)
        self.assertEqual([event.get("text") for event in events if event["type"] == "delta"],
                         ["резерв"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["provider"], "second")

    def test_stream_clean_eof_after_output_is_an_error_without_fallback(self) -> None:
        chunk = json.dumps({"choices": [{"delta": {"content": "часть"}}]}).encode()
        truncated = self._Response([b"data: " + chunk + b"\n"])
        with mock.patch.object(llm, "active_providers", return_value=["first", "second"]), \
             mock.patch.object(llm, "provider_conf", return_value={
                 "api_key": "test", "base_url": "https://provider.invalid",
             }), mock.patch.object(llm, "pick_model", side_effect=lambda _tier, provider: provider + "-model"), \
             mock.patch.object(llm, "_request", return_value=truncated) as request:
            events = list(llm._chat_stream_impl(
                [{"role": "user", "content": "test"}], _span=self._span(),
            ))
        request.assert_called_once()
        self.assertEqual([event["type"] for event in events], ["model", "delta", "error"])
        self.assertIn("[DONE]", events[-1]["error"])

    def test_history_summary_is_local_and_keeps_recent_context(self) -> None:
        messages = [
            {"id": str(index), "role": "user" if index % 2 == 0 else "assistant",
             "content": "реплика %d" % index}
            for index in range(22)
        ]
        orchestrator._SUM_CACHE.clear()
        with mock.patch.object(llm, "chat") as hidden_llm:
            compact = orchestrator.summarize_history(messages, keep_last=10)
        hidden_llm.assert_not_called()
        self.assertEqual(compact[0]["role"], "system")
        self.assertIn("реплика 11", compact[0]["content"])
        self.assertEqual(compact[1:], messages[-10:])

    def test_post_sse_bookkeeping_cannot_start_semantic_llm_or_thread(self) -> None:
        with mock.patch.object(server.orchestrator, "make_chat_title", return_value="Локальный заголовок"), \
             mock.patch.object(server.db, "rename_chat") as rename, \
             mock.patch.object(server.agent, "remember_semantic_facts") as semantic, \
             mock.patch.object(server.threading, "Thread") as thread:
            server._finish_local_post("chat-1", "Длинная тема", title=True)
        rename.assert_called_once_with("chat-1", "Локальный заголовок")
        semantic.assert_not_called()
        thread.assert_not_called()

    def test_ideas_and_proactivity_do_not_use_hidden_llm_calls(self) -> None:
        with mock.patch.object(llm, "chat") as hidden_llm, \
             mock.patch.object(ideas, "_read", return_value={}), \
             mock.patch.object(ideas, "_write") as write, \
             mock.patch.object(ideas.db, "recent_user_messages", return_value=[
                 "Собери сравнительную таблицу поставщиков для проекта",
             ]):
            cards = ideas.refresh(force=True)
        hidden_llm.assert_not_called()
        write.assert_called_once()
        self.assertTrue(cards)
        self.assertIn("следующий конкретный шаг", cards[0]["prompt"])

        with mock.patch.object(llm, "chat") as hidden_llm, \
             mock.patch.object(auto.db, "recent_user_messages", return_value=["Подготовь отчёт"]), \
             mock.patch.object(auto.db, "notify") as notify, \
             mock.patch.object(auto.CONFIG, "get", return_value={}):
            auto.proactive_tick(_reserved=True)
        hidden_llm.assert_not_called()
        notify.assert_called_once()

    def test_web_search_marks_fast_engine_errors_as_partial(self) -> None:
        hit = [{"title": "One", "url": "https://one.example/a", "snippet": "A"}]
        with mock.patch.object(web, "_ddg", return_value=hit), \
             mock.patch.object(web, "_mail_search", side_effect=OSError("blocked")), \
             mock.patch.object(web, "_wikipedia", return_value=[]), \
             mock.patch.object(web.telemetry, "Span") as span_type:
            result = web.web_search("query", count=5)
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertIn("mail_search", result["warnings"][0])
        span_type.return_value.finish.assert_called_once()
        self.assertEqual(span_type.return_value.finish.call_args.args[0], "partial")
        self.assertEqual(span_type.return_value.finish.call_args.kwargs["error_count"], 1)

    def test_web_search_reports_ok_only_after_all_engines_complete(self) -> None:
        ddg = [{"title": "One", "url": "https://one.example/a", "snippet": "A"}]
        mail = [{"title": "Two", "url": "https://two.example/b", "snippet": "B"}]
        with mock.patch.object(web, "_ddg", return_value=ddg), \
             mock.patch.object(web, "_mail_search", return_value=mail), \
             mock.patch.object(web, "_wikipedia", return_value=[]), \
             mock.patch.object(web.telemetry, "Span") as span_type:
            result = web.web_search("query", count=5)
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual([item["title"] for item in result["results"]], ["One", "Two"])
        self.assertEqual(span_type.return_value.finish.call_args.args[0], "ok")

    def test_deep_research_preserves_degraded_search_status(self) -> None:
        found = {
            "ok": True, "partial": True,
            "results": [{"title": "One", "url": "https://one.example/a", "snippet": "A"}],
        }
        page = {"ok": True, "url": "https://one.example/a", "title": "One", "text": "Body"}
        with mock.patch.object(web, "web_search", return_value=found), \
             mock.patch.object(web, "open_url", return_value=page), \
             mock.patch.object(web.telemetry, "Span") as span_type:
            result = web.deep_research("query", pages=1)
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(span_type.return_value.finish.call_args.args[0], "partial")
        self.assertTrue(span_type.return_value.finish.call_args.kwargs["search_partial"])

    def test_telemetry_rotates_and_span_finishes_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "latency.jsonl"
            path.write_text("x" * 80, encoding="utf-8")
            with mock.patch.object(telemetry, "LOG_DIR", folder), \
                 mock.patch.object(telemetry, "_PATH", path), \
                 mock.patch.object(telemetry, "_MAX_BYTES", 32):
                telemetry.emit("web.search", duration_ms=12.5, status="partial",
                               provider="p", model="m", retry=1, error_count=1)
            self.assertTrue(Path(str(path) + ".1").exists())
            row = json.loads(path.read_text("utf-8"))
            self.assertEqual((row["operation"], row["status"], row["retry"]),
                             ("web.search", "partial", 1))

        with mock.patch.object(telemetry, "emit") as emit:
            span = telemetry.Span("foreground")
            span.first_token()
            span.finish("ok")
            span.finish("error")
        emit.assert_called_once()
        self.assertIn("ttft_ms", emit.call_args.kwargs)


class AutoLifecycleRaceTests(unittest.TestCase):
    def tearDown(self) -> None:
        with auto._RUN_LOCK:
            auto._RUNNING.clear()

    def test_paused_task_still_blocks_a_duplicate_prompt(self) -> None:
        tasks = [
            {"id": "paused", "status": "paused", "prompt": "Собери отчёт", "chat_id": "chat-1"},
            {"id": "done", "status": "done", "prompt": "Другой отчёт", "chat_id": "chat-1"},
        ]
        with mock.patch.object(auto.db, "list_tasks", return_value=tasks):
            self.assertTrue(auto.has_similar_pending("  собери   ОТЧЁТ ", "chat-1"))
            self.assertFalse(auto.has_similar_pending("Собери отчёт", "chat-2"))

    def test_two_launchers_can_reserve_one_task_only_once(self) -> None:
        task = {"id": "race-task", "status": "queued", "prompt": "work"}
        results = []
        gate = __import__("threading").Barrier(3)

        def reserve():
            gate.wait()
            results.append(auto._reserve_task("race-task"))

        with mock.patch.object(auto.CONFIG, "get", return_value=False), \
             mock.patch.object(auto.db, "get_task", return_value=task), \
             mock.patch.object(auto.db, "update_task") as update, \
             mock.patch.object(auto.db, "append_task_event"), \
             mock.patch.object(auto, "MAX_PARALLEL", 1):
            threads = [__import__("threading").Thread(target=reserve) for _ in range(2)]
            for thread in threads:
                thread.start()
            gate.wait()
            for thread in threads:
                thread.join(2)
        self.assertEqual(sum(result is not None for result in results), 1)
        update.assert_called_once_with(
            "race-task", status="running", resume_status="", progress=0.05,
        )

    def test_pause_cancellation_wins_over_a_late_worker_result(self) -> None:
        task = {"id": "late-task", "title": "Late", "prompt": "work",
                "status": "running", "schedule": "", "chat_id": "chat-1"}
        cancelled = __import__("threading").Event()
        with auto._RUN_LOCK:
            auto._RUNNING[task["id"]] = cancelled

        def finish_late(*_args, **_kwargs):
            cancelled.set()
            return {"content": "late answer", "files": []}

        with mock.patch.object(auto, "safe_delayed_message", return_value=None), \
             mock.patch.object(auto.agent, "run_headless", side_effect=finish_late), \
             mock.patch.object(auto.db, "update_task") as update, \
             mock.patch.object(auto.db, "add_message") as add_message, \
             mock.patch.object(auto.db, "append_task_event"):
            auto._execute_reserved(task, cancelled)
        update.assert_not_called()
        add_message.assert_not_called()
        self.assertNotIn(task["id"], auto._RUNNING)

    def test_pause_and_resume_preserve_each_tasks_intended_state(self) -> None:
        future = time.time() + 600
        tasks = [
            {"id": "queued", "status": "queued", "resume_status": "", "next_run": 0},
            {"id": "scheduled", "status": "scheduled", "resume_status": "", "next_run": future},
            {"id": "done", "status": "done", "resume_status": "", "next_run": 0},
        ]

        def update(task_id, **fields):
            item = next(item for item in tasks if item["id"] == task_id)
            item.update(fields)

        with mock.patch.object(auto.db, "list_tasks", side_effect=lambda **_kwargs: tasks), \
             mock.patch.object(auto.db, "update_task", side_effect=update), \
             mock.patch.object(auto.CONFIG, "set") as set_config:
            self.assertEqual(auto.pause_all(), 2)
            self.assertEqual([item["status"] for item in tasks], ["paused", "paused", "done"])
            self.assertEqual([item["resume_status"] for item in tasks[:2]],
                             ["queued", "scheduled"])
            self.assertEqual(auto.resume_all(), 2)
        self.assertEqual([item["status"] for item in tasks], ["queued", "scheduled", "done"])
        self.assertEqual(set_config.call_args_list, [
            mock.call("auto.paused", True), mock.call("auto.paused", False),
        ])


if __name__ == "__main__":
    unittest.main()
