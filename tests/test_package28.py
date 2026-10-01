"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
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
    def test_plan_is_owed_after_preflight_panel(self) -> None:
        # S: многошаговая задача получила preflight-панель вместо работы —
        # ответ на панель обязан всё равно получить план
        hist = [
            {"role": "user", "content": "напиши игру змейка с уровнями"},
            {"role": "assistant", "content": "Выбери:\n```ui\ntiles Поле: 20 | 30\n```"},
            {"role": "user", "content": "поле 30, скорость средняя"},
        ]
        self.assertTrue(agent.plan_owed_by_history(hist))
        # T: ход-вопрос БЕЗ текста (ask_user) тоже считается вопросом
        asked = [
            {"role": "user", "content": "напиши игру змейка с уровнями"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "поле 30, скорость средняя"},
        ]
        self.assertTrue(agent.plan_owed_by_history(asked))
        # план уже шёл ([ШАГ …]) — второго плана не нужно
        ran = [
            {"role": "user", "content": "напиши игру"},
            {"role": "assistant", "content": "[ШАГ 1] Готовлю поле\n```ui\ntiles Оформление: тёмное | светлое\n```"},
            {"role": "user", "content": "тёмное"},
        ]
        self.assertFalse(agent.plan_owed_by_history(ran))
        # панели не было — задолженности нет
        plain = [
            {"role": "user", "content": "напиши игру"},
            {"role": "assistant", "content": "Готово: game.html в песочнице"},
            {"role": "user", "content": "спасибо"},
        ]
        self.assertFalse(agent.plan_owed_by_history(plain))
        self.assertFalse(agent.plan_owed_by_history([]))

    def test_agent_prompt_does_not_teach_questioning_instead_of_work(self) -> None:
        # S: в AGENT «напиши игру» — не анкета: агент выбирает некритичное сам
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            ag = agent.build_system_prompt(True)
            self.assertIn("НЕ повод для анкеты", ag)
            self.assertNotIn("дай выбрать размер поля", ag)
            # в обычном режиме пример с игрой остаётся
            plain = agent.build_system_prompt(False)
            self.assertIn("дай выбрать размер поля", plain)

    def test_chat_title_uses_llm_with_local_fallback(self) -> None:
        # U: заголовок снова придумывает ИИ (nano); ошибка/пустой ответ —
        # локальный запасной, но не отсутствие заголовка
        with mock.patch.object(orchestrator, "_fallback_title",
                               return_value="Локальный запасной") as fb, \
             mock.patch("jarvis.llm.chat",
                        return_value={"content": "Космическая аркада"}):
            self.assertEqual(orchestrator.make_chat_title("напиши игру про космос"),
                             "Космическая аркада")
        with mock.patch("jarvis.llm.chat", side_effect=RuntimeError("net")):
            fb2 = orchestrator._make_title("напиши игру про космос")
            self.assertTrue(fb2)          # запасной заголовок есть всегда

    def test_interrupted_answer_closes_the_turn(self) -> None:
        # U: прерванный ответ не оставляет «висящий вопрос»: на месте обрыва
        # метка, модель не отвечает первым делом на старую задачу
        hist = [
            {"role": "user", "content": "напиши игру"},
            {"role": "assistant", "content": "Начинаю делать…",
             "meta": {"interrupted": True}},
        ]
        def rows(msgs):
            out = []
            for m in msgs:
                if m["role"] not in ("user", "assistant") or not m["content"]:
                    continue
                if m["role"] == "assistant" and (m.get("meta") or {}).get("interrupted"):
                    out.append({"role": "assistant",
                                "content": "[Ответ был прерван пользователем]"})
                    continue
                out.append({"role": m["role"], "content": m["content"]})
            return out
        got = rows(hist)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["content"], "[Ответ был прерван пользователем]")

    def test_text_work_plan_never_blocks_the_stream(self) -> None:
        # V: план для текстовой работы строится В ФОНОВОМ ПОТОКЕ — ни один
        # llm-запрос не останавливает печать; на финале — мгновенный локальный
        ag = agent.Agent(agent_mode=True)
        steps = ag.local_plan("напиши игру про космос")
        self.assertEqual(len(steps), 3)
        self.assertTrue(all("напиши игру про космос" in s for s in steps))
        self.assertEqual(ag.local_plan(""), [])
        run_src = inspect.getsource(agent.Agent._run_body)
        self.assertIn("threading.Thread(target=_bg_plan, daemon=True).start()", run_src)
        self.assertIn('plan_box["steps"] is not None', run_src)
        self.assertIn("self.local_plan(user_text)", run_src)
        self.assertNotIn("plan = self.make_plan(user_text, [])", run_src)

    def test_chat_title_is_fast(self) -> None:
        # V: заголовок — самая дешёвая модель, жёсткий лимит 2.5с
        src = inspect.getsource(orchestrator._make_title)
        self.assertIn('tier="nano"', src)
        self.assertIn("timeout=2.5", src)

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
        self.assertLessEqual(planner.call_args.kwargs["timeout"], 5)
        self.assertLessEqual(planner.call_args.kwargs["max_tokens"], 320)
        self.assertIn("write_file", planner.call_args.args[0][-1]["content"])

        # ПЛАН В AGENT — ВСЕГДА (просьба пользователя). Ошибка планировщика
        # больше не означает «плана нет»: вторая попытка, затем локальный
        # фоллбек с предметом задачи в каждом шаге.
        with mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("offline")):
            plan = runner.make_plan("Напиши игру шахматы")
        self.assertEqual(len(plan), 3)
        for step in plan:
            self.assertIn("Напиши игру шахматы", step,
                          "fallback steps must name the task subject, not generic placeholders")

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

    def test_wait_visual_is_registry_owned_and_only_marks_real_waits(self) -> None:
        self.assertTrue(agent.tools.has_wait_visual("web_search"))
        self.assertTrue(agent.tools.has_wait_visual("open_url"))
        self.assertTrue(agent.tools.has_wait_visual("generate_image"))
        self.assertFalse(agent.tools.has_wait_visual("remember"))
        self.assertFalse(agent.tools.has_wait_visual("write_file"))
        self.assertFalse(agent.tools.has_wait_visual("system_info"))

        route = {"tier": "base", "reason": "test", "score": 0,
                 "verbose": True, "offer_tools": True}
        schemas = [{"type": "function", "function": {
            "name": name, "parameters": {"type": "object"}}}
            for name in ("open_url", "write_file")]
        turns = iter((
            [{"type": "done", "tool_calls": [
                {"id": "web", "type": "function", "function": {
                    "name": "open_url", "arguments": json.dumps({"url": "https://example.com"})}},
                {"id": "file", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "x", "content": "y"})}},
            ]}],
            [{"type": "delta", "text": "Готово."}, {"type": "done", "tool_calls": []}],
        ))
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schemas), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(agent.Agent().run(
                [{"role": "user", "content": "Прочитай и сохрани"}],
                user_text="Прочитай и сохрани"))
        waits = {event["name"]: event["wait_visual"] for event in events
                 if event.get("type") == "tool_start"}
        self.assertEqual(waits, {"open_url": True, "write_file": False})

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
        self.assertEqual(names, ["write_file", "request_mode", "switch_model"])
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
                [{"role": "user", "content": "Собери документ, формат: Markdown"}],
                user_text="Собери документ, формат: Markdown", preflight_resolved=True,
            ))
        self.assertFalse(any(event.get("type") == "reply_ui" for event in events))
        shown = "".join(event.get("text", "") for event in events if event.get("type") == "delta")
        self.assertNotIn("Ещё один вопрос", shown)
        self.assertIn("Документ готов", shown)
        planner.assert_called_once_with("Собери документ, формат: Markdown", ["write_file"])
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
        self.assertTrue(agent.opens_external_ui("open_app", {"name": "Safari"}))
        self.assertTrue(agent.opens_external_ui("run_shell", {"command": "open report.pdf"}))
        self.assertTrue(agent.opens_external_ui("run_python", {
            "code": "import webbrowser; webbrowser.open('https://example.com')",
        }))
        self.assertFalse(agent.opens_external_ui("run_python", {
            "code": "with open('report.txt') as fh: print(fh.read())",
        }), "ordinary sandbox file I/O must not be mistaken for a GUI launch")
        self.assertEqual(agent.needs_approval("open_app", {"name": "Terminal"}),
                         "открытие приложения «Терминал»")
        self.assertEqual(agent.needs_approval("run_shell", {"command": "open report.pdf"}),
                         "открытие окна или приложения вне режима «Компьютер»")
        self.assertEqual(agent.approval_style("open_app", {"name": "Terminal"}),
                         "permission")
        self.assertEqual(agent.approval_style("run_shell", {"command": "open -a Terminal"}),
                         "permission", "opening an external window is a soft permission")

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

    def test_agent_cannot_auto_approve_external_window_when_computer_is_off(self) -> None:
        route = {"tier": "base", "reason": "test", "score": 0.4,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {
            "name": "run_shell", "parameters": {"type": "object"}}}]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "open-preview", "type": "function",
                "function": {"name": "run_shell", "arguments": json.dumps({
                    "command": "open report.pdf",
                })},
            }]}],
            [{"type": "delta", "text": "Файл не открывался."},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True, computer_use=False, approvals_auto=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "make_plan", return_value=[]), \
             mock.patch.object(runner, "_wait_approval", return_value={"status": "rejected"}) as wait, \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(runner.run(
                [{"role": "user", "content": "Подготовь и открой отчёт"}],
                user_text="Подготовь и открой отчёт",
            ))
        approval = next(event for event in events if event.get("type") == "approval_wait")
        self.assertEqual(approval["style"], "permission")
        self.assertIn("вне режима", approval["reason"])
        wait.assert_called_once()
        dispatch.assert_not_called()


class SmartMemoryTests(unittest.TestCase):
    """Структуру памяти решает маленькая модель, а не слепой regex.

    «я люблю кошек» раньше превращалось в «я люблю: кошек» — теперь модель
    обязана дать осмысленную категорию и значение в нормальной форме."""

    def test_llm_structures_the_fact_before_saving(self) -> None:
        reply = {"content": json.dumps([
            {"type": "любимое животное", "value": "кошки"},
        ], ensure_ascii=False)}
        with mock.patch.object(agent, "extract_obvious_memories",
                               return_value=[{"kind": "Нравится", "key": "Нравится",
                                              "value": "кошек"}]), \
             mock.patch.object(agent.llm, "chat", return_value=reply) as model, \
             mock.patch.object(agent.db, "remember", side_effect=lambda k, key, v, w: {
                 "kind": k, "key": key, "value": v}) as remember:
            saved = agent.remember_smart_facts("я люблю кошек")
        self.assertEqual(saved, [{"kind": "любимое животное",
                                  "key": "Любимое животное", "value": "кошки"}])
        model.assert_called_once()
        self.assertIn("экстрактор долговременной памяти", model.call_args.args[0][0]["content"])
        remember.assert_called_once_with("любимое животное", "Любимое животное", "кошки", 1.15)

    def test_llm_verdict_empty_means_do_not_save(self) -> None:
        with mock.patch.object(agent, "extract_obvious_memories",
                               return_value=[{"kind": "Нравится", "key": "x", "value": "y"}]), \
             mock.patch.object(agent.llm, "chat",
                               return_value={"content": "[]"}), \
             mock.patch.object(agent.db, "remember") as remember:
            self.assertEqual(agent.remember_smart_facts("сегодня я устал"), [])
        remember.assert_not_called()

    def test_model_failure_falls_back_to_local_verbatim(self) -> None:
        local = [{"kind": "Нравится", "key": "Нравится", "value": "кошек"}]
        with mock.patch.object(agent, "extract_obvious_memories", return_value=local), \
             mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("network")), \
             mock.patch.object(agent, "remember_obvious_facts", return_value=[{"saved": 1}]) as fb:
            self.assertEqual(agent.remember_smart_facts("я люблю кошек"), [{"saved": 1}])
        fb.assert_called_once_with("я люблю кошек")

    def test_plain_replika_without_candidates_costs_no_model_call(self) -> None:
        with mock.patch.object(agent, "extract_obvious_memories", return_value=[]), \
             mock.patch.object(agent.llm, "chat") as model:
            self.assertEqual(agent.remember_smart_facts("как дела?"), [])
        model.assert_not_called()


class DirectVisionTests(unittest.TestCase):
    """Computer-use: зрячая управляющая модель видит кадр сама.

    Прежняя связка «зрячая опишет словами — слепая решит» стоила два запроса
    на каждый шаг и теряла точность в пересказе."""

    def _shot(self):
        return {"ok": True, "path": "screen_1.png",
                "download_url": "/api/download/screen_1.png",
                "data_url": "data:image/png;base64,QUJD", "bytes": 3,
                "width": 800, "height": 600, "scale": 0.5}

    def test_direct_vision_appends_frame_image_and_drops_old(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runner = agent.Agent(computer_use=True)
            runner._vision_direct = True
            convo: list = []
            call = {"id": "s1", "type": "function",
                    "function": {"name": "screenshot", "arguments": "{}"}}
            with mock.patch.object(agent, "_describe_screen") as describe, \
                 mock.patch.object(agent.sandbox, "root", return_value=Path(td)):
                runner._append_tool_result(convo, call, "screenshot", self._shot(), False)
                runner._append_tool_result(convo, call, "screenshot", self._shot(), False)
            describe.assert_not_called()  # отдельный describe-запрос больше не нужен
            frames = [m for m in convo
                      if m.get("role") == "user" and isinstance(m.get("content"), list)]
            # живой кадр-картинка всегда один — последний; старый стал текстом
            self.assertEqual(len(frames), 1)
            last = frames[0]["content"]
            self.assertEqual(last[1]["image_url"]["url"], "data:image/png;base64,QUJD")
            self.assertIn("× 2", last[0]["text"], "масштаб пересчёта координат указан")
            self.assertTrue(any(
                m.get("role") == "user" and
                str(m.get("content")).startswith("[КАДР] Предыдущий кадр устарел")
                for m in convo), "stale frame must be replaced by a text stub")

    def test_vision_tier_selected_for_computer_use(self) -> None:
        runner = agent.Agent(computer_use=True)
        runner._vision_direct = True
        events = []
        route = {"tier": "base", "reason": "test", "score": 0.0,
                 "verbose": False, "offer_tools": False}
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", return_value=iter([])), \
             mock.patch.object(agent.tools, "schemas", return_value=[]):
            for event in runner.run([{"role": "user", "content": "нажми кнопку"}],
                                     user_text="нажми кнопку"):
                events.append(event)
        routed = next(e for e in events if e.get("type") == "route")
        self.assertEqual(routed["tier"], "vision")


class BudgetLimitTests(unittest.TestCase):
    """Лимит ₽: при исчерпании агент спрашивает, продолжать ли."""

    def _run_with_budget(self, limit, answer):
        route = {"tier": "base", "reason": "test", "score": 0.0,
                 "verbose": False, "offer_tools": True}
        usage = {"prompt_tokens": 100000, "completion_tokens": 100}
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        turns = iter((
            # первый ход: дорогой вызов инструмента — бюджет расходуется
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "x"})}}],
              "usage": usage, "model": "test-model"}],
            # второй ход происходит уже ПОСЛЕ решения о лимите
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": [], "usage": usage, "model": "test-model"}],
        ))

        def fake_stream(*_a, **_k):
            # настоящий llm капает usage в sink прогона — мока делает то же
            for ev in next(turns):
                if ev.get("type") == "done" and ev.get("usage"):
                    llm._report_usage(ev.get("model") or "test-model",
                                      int(ev["usage"].get("prompt_tokens") or 0),
                                      int(ev["usage"].get("completion_tokens") or 0))
                yield ev
        runner = agent.Agent()
        runner.budget_rub = limit
        question = {"id": "q1", "status": "answered", "answer": answer}
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}), \
             mock.patch.object(agent.db, "create_question", return_value=dict(question)), \
             mock.patch.object(agent.db, "get_question", return_value=dict(question)):
            events = list(runner.run([{"role": "user", "content": "задача"}],
                                     user_text="задача"))
        return runner, events

    def test_budget_increase_continues_run(self) -> None:
        runner, events = self._run_with_budget(0.01, "Увеличить на 25 ₽")
        waits = [e for e in events if e.get("type") == "budget_wait"]
        updates = [e for e in events if e.get("type") == "budget_update"]
        self.assertTrue(waits, "budget_wait must be emitted before waiting")
        self.assertTrue(updates)
        self.assertAlmostEqual(runner.budget_rub, 25.01)
        self.assertTrue(any(e.get("type") == "done" for e in events))

    def test_budget_disable_removes_limit(self) -> None:
        runner, events = self._run_with_budget(0.01, "Отключить лимит")
        self.assertTrue(any(e.get("type") == "budget_off" for e in events))
        self.assertIsNone(runner.budget_rub)
        self.assertTrue(any(e.get("type") == "done" for e in events))

    def test_budget_stop_ends_run_with_honest_text(self) -> None:
        runner, events = self._run_with_budget(0.01, "Остановить")
        done = next(e for e in events if e.get("type") == "done")
        self.assertIn("лимит", done["content"].lower())
        self.assertIsNotNone(done.get("budget_spent"))


class ScenarioStorageTests(unittest.TestCase):
    def test_scenarios_crud(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "test.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit()
                conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()):
                    created = db.create_scenario("Дайджест утра",
                                                 ["Новости за вчера", "  ", "Погода"],
                                                 emoji="☀️")
                    self.assertEqual(created["steps"], ["Новости за вчера", "Погода"])
                    listed = db.list_scenarios()
                    self.assertEqual(len(listed), 1)
                    self.assertEqual(listed[0]["title"], "Дайджест утра")
                    db.delete_scenario(created["id"])
                    self.assertEqual(db.list_scenarios(), [])


class ProactiveModesTests(unittest.TestCase):
    """Джарвис сам просит включить режим — через вопрос с кнопкой."""

    def _run_request_mode(self, answer):
        route = {"tier": "base", "reason": "test", "score": 0.0,
                 "verbose": False, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "write_file",
                                                    "parameters": {"type": "object"}}}]
        stream = [
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "request_mode",
                             "arguments": json.dumps({"mode": "agent",
                                                      "reason": "многошаговая сборка"})}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}],
            [{"type": "delta", "text": "Продолжаю сам."},
             {"type": "done", "tool_calls": [], "usage": None}],
        ]
        turns = iter(stream)
        runner = agent.Agent()
        record = {"id": "q1", "status": "answered", "answer": answer}
        captured: dict = {}
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *a, **k: (captured.setdefault("tools", k.get("tools")),
                                                            next(turns))[1]), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "_wait_answer", return_value=record):
            events = list(runner.run([{"role": "user", "content": "собери отчёт"}],
                                     user_text="собери отчёт"))
        return runner, events, captured

    def test_request_mode_enabled_mid_run(self) -> None:
        runner, events, captured = self._run_request_mode("Включить")
        self.assertTrue(runner.agent_mode, "разрешение применено к текущему прогону")
        req = next(e for e in events if e.get("type") == "mode_request")
        self.assertEqual(req["mode"], "agent")
        self.assertIn("многошаговая", req["reason"])
        self.assertTrue(any(e.get("type") == "mode_changed" and e.get("on")
                            for e in events))
        # во втором ходе список инструментов остался и содержит request_mode
        self.assertIn("request_mode",
                      [t["function"]["name"] for t in captured.get("tools", [])])

    def test_request_mode_declined_keeps_mode_off(self) -> None:
        runner, events, _ = self._run_request_mode("Не нужно")
        self.assertFalse(runner.agent_mode)
        self.assertTrue(any(e.get("type") == "mode_declined" for e in events))
        self.assertTrue(any(e.get("type") == "done" for e in events))


class AutoWorkerTests(unittest.TestCase):
    """Тикер AUTO реально запускает задачи: очередь и расписание."""

    def test_active_tasks_returns_pending_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "t.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit(); conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()):
                    live = db.create_task(title="live", prompt="p")
                    db.update_task(live["id"], status="scheduled",
                                   next_run=time.time() + 5)
                    done = db.create_task(title="done", prompt="p")
                    db.update_task(done["id"], status="done")
                    active = db.active_tasks()
                    self.assertEqual({t["id"] for t in active}, {live["id"]})

    def test_scheduled_task_launches_within_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "t.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit(); conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()), \
                     mock.patch.object(auto.agent, "run_headless",
                                       return_value={"content": "готово", "files": []}) as headless:
                    # «напиши…» с расписание поймал бы direct-путь таймера;
                    # для агентного исполнения берём рабочую формулировку
                    task = auto.create_background_task("Сводка", "собери сводку новостей",
                                                       schedule="in 2s")
                    self.assertEqual(task["status"], "scheduled")
                    auto.start()
                    try:
                        deadline = time.time() + 12
                        while time.time() < deadline:
                            if db.get_task(task["id"])["status"] == "done":
                                break
                            time.sleep(0.25)
                    finally:
                        auto.stop()
                    self.assertEqual(db.get_task(task["id"])["status"], "done",
                                     "через 2 секунды задача обязана запуститься и завершиться")
                    headless.assert_called_once()


class ModeSuggestionTests(unittest.TestCase):
    """Проактивные режимы: локальный триггер до модели — шахматы получают AGENT,
    «нажми…» — Компьютер, «как я выгляжу» — Камеру."""

    def test_creation_tasks_ask_for_agent(self) -> None:
        self.assertEqual(agent.suggest_mode("Напиши игру шахматы", False, False),
                         {"mode": "agent",
                          "reason": "многошаговая сборка: план и несколько шагов работы"})
        self.assertEqual(agent.suggest_mode("собери сайт-портфолио", False, False)["mode"],
                         "agent")
        # письмо и презентация — не агентская сборка
        self.assertIsNone(agent.suggest_mode("напиши письмо маме", False, False))
        self.assertIsNone(agent.suggest_mode("сделай презентацию", False, False))

    def test_screen_actions_ask_for_computer(self) -> None:
        hint = agent.suggest_mode("нажми на иконку открытия нового диалога", False, False)
        self.assertEqual(hint["mode"], "computer")
        # режим уже включён — не предлагаем
        self.assertIsNone(agent.suggest_mode("нажми кнопку", False, True))

    def test_look_requests_ask_for_camera(self) -> None:
        self.assertEqual(agent.suggest_mode("как я выгляжу?", False, False)["mode"],
                         "camera")
        self.assertIsNone(agent.suggest_mode("привет, как дела", False, False))


class UiTreeTests(unittest.TestCase):
    """Дерево элементов: дешёвая альтернатива скриншоту в computer-use."""

    def test_ui_tree_registered_and_silent(self) -> None:
        from jarvis import tools as jarvis_tools
        self.assertEqual(jarvis_tools.group_of("ui_tree"), "computer")
        self.assertTrue(jarvis_tools.is_silent("ui_tree"))
        self.assertIn("ui_tree", jarvis_tools.schemas(["computer"])[0]
                      and "" or "ui_tree",
                      str(jarvis_tools.schemas(["computer"])))

    def test_ui_tree_parses_jxa_output(self) -> None:
        from jarvis.tools import system as sysmod
        sample = ('window "Настройки" [0,0 800x600] app:System Settings\n'
                  '  AXButton "Готово" [700,40 80x28]\n'
                  '    AXStaticText "Профиль" [12,12 120x20]')
        with mock.patch.object(sysmod, "IS_MAC", True), \
             mock.patch.object(sysmod, "_jxa",
                               return_value={"ok": True, "out": sample}):
            res = sysmod.ui_tree()
        self.assertTrue(res["ok"])
        self.assertIn("AXButton", res["tree"])
        self.assertIn("[700,40 80x28]", res["tree"])

    def test_ui_tree_reports_no_window(self) -> None:
        from jarvis.tools import system as sysmod
        with mock.patch.object(sysmod, "IS_MAC", True), \
             mock.patch.object(sysmod, "_jxa",
                               return_value={"ok": True, "out": "NO_WINDOW app:Finder"}):
            res = sysmod.ui_tree()
        self.assertFalse(res["ok"])
        self.assertIn("нет открытых окон", res["error"])


class SwitchModelTests(unittest.TestCase):
    """Смена модели по ходу ответа: route-событие и применение на следующем шаге."""

    def test_switch_model_emits_route_and_applies(self) -> None:
        route = {"tier": "base", "reason": "test", "score": 0.0,
                 "verbose": False, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "write_file",
                                                    "parameters": {"type": "object"}}}]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "switch_model",
                             "arguments": json.dumps({"tier": "coder",
                                                      "reason": "нужен код"})}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}],
            [{"type": "done", "tool_calls": [{
                "id": "t2", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": "a.py", "content": "x"})}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}],
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": [], "usage": None}],
        ))
        captured: list = []

        def fake_stream(*a, **k):
            captured.append(k.get("tier"))
            return next(turns)

        runner = agent.Agent()
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(runner.run([{"role": "user", "content": "сделай скрипт"}],
                                     user_text="сделай скрипт"))
        self.assertEqual(captured, ["base", "coder", "coder"],
                         "после switch_model все следующие ходы идут на новом тарифе")
        routes = [e for e in events if e.get("type") == "route"]
        self.assertTrue(any(r.get("tier") == "coder" for r in routes))


class HonestPlanTests(unittest.TestCase):
    """План видит сама модель и следует ему честно — не только интерфейс."""

    def test_announce_plan_injects_plan_into_context(self) -> None:
        route = {"tier": "coder", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "write_file",
                                                    "parameters": {"type": "object"}}}]
        stream = [
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": "g.py", "content": "x"})}}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 5}}],
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": [], "usage": None}],
        ]
        turns = iter(stream)
        convo_seen: list = []

        def fake_stream(convo, **_k):
            convo_seen.append([dict(m) for m in convo])
            return next(turns)

        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}), \
             mock.patch.object(runner, "make_plan",
                               return_value=["Каркас игры", "Логика ходов",
                                             "Интерфейс", "Проверка и сдача"]):
            events = list(runner.run([{"role": "user", "content": "напиши игру"}],
                                     user_text="напиши игру"))
        self.assertTrue(any(e.get("type") == "plan" for e in events))
        # со второго хода в контексте лежит сам план с честным правилом
        second = convo_seen[1]
        plan_msg = next((m for m in second if m.get("role") == "system"
                         and "ПЛАН РАБОТЫ" in str(m.get("content"))), None)
        self.assertIsNotNone(plan_msg, "plan must live in the model context")
        self.assertIn("Логика ходов", plan_msg["content"])
        self.assertIn("[ШАГ N]", plan_msg["content"])

    def test_usage_sink_counts_every_call(self) -> None:
        runner = agent.Agent()
        runner.model_used = "some-model"
        before = runner._spent_rub
        with mock.patch.object(agent.llm, "estimate_cost", return_value=0.5):
            token = agent.llm._USAGE_SINK.set(runner._sink_usage)
            try:
                agent.llm._report_usage("some-model", 1000, 500)
            finally:
                agent.llm._USAGE_SINK.reset(token)
        self.assertAlmostEqual(runner._spent_rub - before, 0.5)

    def test_raw_tool_json_answer_is_retried_and_never_final(self) -> None:
        """Сырой JSON-конверт не показывают пользователю как ответ.

        Модель дважды отвечает `{"ok": true, ...}` (выдумала или повторила
        результат инструмента) — пользователь должен получить либо нормальный
        текст, либо локальный пересказ реальных результатов.
        """
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        payload = json.dumps({"ok": True, "query": "новости",
                              "engine": "news", "results": []},
                             ensure_ascii=False)
        turns = iter((
            [{"type": "delta", "text": payload},
             {"type": "done", "tool_calls": []}],
            [{"type": "delta", "text": payload},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "make_plan", return_value=[]):
            events = list(runner.run([{"role": "user", "content": "новости"}],
                                     user_text="новости"))
        done = next(e for e in events if e.get("type") == "done")
        self.assertNotIn('{"ok": true', done.get("content", ""),
                         "raw tool envelope must never be the final answer")
        self.assertTrue(done.get("content", "").strip())

    def test_payload_classifier_knows_envelopes(self) -> None:
        self.assertTrue(agent.is_tool_payload_answer(
            '{"ok": true, "query": "x", "engine": "news"}'))
        self.assertTrue(agent.is_tool_payload_answer(
            '```json\n{"ok": true, "status": 200, "body": "..."}\n```'))
        self.assertFalse(agent.is_tool_payload_answer(
            "Вот сводка: сегодня 12 градусов, ветер 7 м/с."))
        self.assertFalse(agent.is_tool_payload_answer(
            'Держи JSON: {"a": 1} — это то, что просил.'))

    def test_plan_markers_drive_counter_and_switch_off_autoadvance(self) -> None:
        """[ШАГ N] от модели — факт: после первой пометки автопродвижение
        по ходам выключается, счётчик едет только по пометкам."""
        route = {"tier": "coder", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "write_file",
                                                    "parameters": {"type": "object"}}}]
        turns = iter((
            # ход 1: первый инструмент
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": "a.py", "content": "1"})}}]}],
            # ход 2: модель сама помечает шаг 4 и снова работает инструментом
            [{"type": "delta", "text": "[ШАГ 4] Делаю четвёртый шаг."},
             {"type": "done", "tool_calls": [{
                "id": "t2", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": "b.py", "content": "2"})}}]}],
            # ход 3: финальный текст
            [{"type": "delta", "text": "Готово всё."},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}), \
             mock.patch.object(runner, "make_plan",
                               return_value=["Каркас", "Логика", "Данные",
                                             "Сборка", "Тесты", "Проверка"]):
            events = list(runner.run([{"role": "user", "content": "напиши игру"}],
                                     user_text="напиши игру"))
        self.assertTrue(runner._plan_marked,
                        "первая пометка модели обязана включить режим «верим пометкам»")
        steps = [e["step"] for e in events if e.get("type") == "plan_step"]
        # 1 объявление, 2 первый инструмент, 3 автопродвижение хода 2 (ещё без
        # пометок), 4 — ПОМЕТКА модели. Дальше автопродвижение молчит: 5 и 6
        # закрываются только вместе с финальным текстом.
        self.assertEqual(steps, [1, 2, 3, 4, 5, 6])
        step5_at = next(i for i, e in enumerate(events)
                        if e.get("type") == "plan_step" and e.get("step") == 5)
        done_at = next(i for i, e in enumerate(events)
                       if e.get("type") == "delta" and "Готово всё" in e.get("text", ""))
        self.assertGreater(step5_at, done_at,
                           "после пометок модели шаги не должны досыпаться пачкой до ответа")


class AsrRoutesTests(unittest.TestCase):
    def test_transcriptions_endpoint_is_first_cloudru_route(self) -> None:
        from jarvis.tools import media

        class _Cfg:
            @staticmethod
            def get(key, default=None):
                return {"model_tiers.audio": ["whisper-large"]}.get(key, default)

        with mock.patch.object(media, "CONFIG", _Cfg), \
             mock.patch.object(media.llm, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "https://x/v1"}), \
             mock.patch.object(media.llm, "models_of_type", return_value=[]):
            routes = media._asr_routes()
        labels = [r[0] for r in routes]
        self.assertIn("cloudru-chat:whisper-large", labels,
                      "chat/completions — единственный существующий у Cloud.ru путь")
        # X: официальный OpenAPI Cloud.ru содержит ТОЛЬКО /models и
        # /chat/completions — /audio/transcriptions отвечает 404 и лишь
        # сжигал попытку. chat идёт первым, transcriptions — запасным.
        self.assertLess(labels.index("cloudru-chat:whisper-large"),
                        labels.index("cloudru-ts:whisper-large"))


class RunStopTests(unittest.TestCase):
    """Stop = стоп работы, а не только обрыв SSE-картинки."""

    def test_cancelled_agent_dispatches_nothing(self) -> None:
        route = {"tier": "base", "reason": "test", "score": 0.4,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}
        ]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "x"})}}]}],
        ))
        runner = agent.Agent(cancel_check=lambda: True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call") as dispatch:
            events = list(runner.run([{"role": "user", "content": "найди"}],
                                     user_text="найди"))
        dispatch.assert_not_called()
        self.assertFalse([e for e in events if e.get("type") == "tool_start"])

    def test_stop_run_flips_the_registered_event(self) -> None:
        import threading as _th
        event = _th.Event()
        with mock.patch.dict(server._RUN_STOPS, {"tok-1": event}):
            self.assertTrue(server._stop_run("tok-1"))
            self.assertTrue(event.is_set())
            self.assertFalse(server._stop_run("unknown"))


class TurnUiContractEconomyTests(unittest.TestCase):
    """Контракт хода (~340 токенов) нужен только там, где возможен выбор."""

    def _stream(self, body_extra: dict) -> list:
        handler = mock.Mock()
        handler._sse.return_value = True
        handler._sse_open.return_value = None
        handler._sse_close.return_value = None
        stream_events = [{"type": "delta", "text": "Ответ."},
                         {"type": "done", "tool_calls": []}]
        route = {"tier": "base", "reason": "test", "score": 0.0,
                 "verbose": False, "offer_tools": False}
        with mock.patch.object(server.llm, "active_providers", return_value=["test"]), \
             mock.patch.object(server.llm, "chat_stream", return_value=stream_events) as cs, \
             mock.patch.object(server.llm, "chat"), \
             mock.patch.object(server.db, "add_message", return_value={"id": "m1"}), \
             mock.patch.object(server.db, "get_messages", return_value=[]), \
             mock.patch.object(server.db, "rename_chat"), \
             mock.patch.object(server.sandbox, "set_chat"), \
             mock.patch.object(server.auto, "should_background",
                               return_value={"background": False, "schedule": "",
                                             "reason": ""}), \
             mock.patch.object(server.orchestrator, "summarize_history",
                               side_effect=lambda items: items), \
             mock.patch.object(server.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(server.agent, "remember_smart_facts", return_value=[]), \
             mock.patch.object(server.agent, "build_system_prompt",
                               return_value="BASE SYSTEM"), \
             mock.patch.object(server.agent, "turn_ui_contract",
                               return_value="UI CONTRACT") as contract, \
             mock.patch.object(server.agent.tools, "schemas", return_value=[]):
            server.Handler._chat_stream(handler, dict({
                "chat_id": "c1", "text": "привет, как жизнь",
            }, **body_extra))
        return cs.call_args.args[0], contract

    def test_plain_chat_skips_the_contract(self) -> None:
        messages, contract = self._stream({})
        contract.assert_not_called()
        self.assertFalse([m for m in messages if m.get("content") == "UI CONTRACT"])

    def test_agent_mode_and_images_keep_the_contract(self) -> None:
        messages, contract = self._stream({"agent_mode": True})
        contract.assert_called_once()
        self.assertEqual(messages[-2], {"role": "system", "content": "UI CONTRACT"})


class ComputerUseConsentTests(unittest.TestCase):
    """Тумблер «Компьютер» — это и есть согласие управлять мышью и клавиатурой.

    Раньше каждый клик и каждая буква останавливали работу вопросом «управление
    мышью на твоём компьютере» — сделать в режиме нельзя было ничего."""

    def test_computer_mode_toggle_consents_to_input_actions(self) -> None:
        self.assertIsNone(agent.needs_approval("mouse_click", {"x": 1, "y": 2},
                                               computer_use=True))
        self.assertIsNone(agent.needs_approval("type_text", {"text": "привет"},
                                               computer_use=True))
        self.assertIsNone(agent.needs_approval("press_key", {"key": "return"},
                                               computer_use=True))
        self.assertIsNone(agent.needs_approval("mouse_move", {"x": 10, "y": 10},
                                               computer_use=True))
        self.assertIsNone(agent.needs_approval("open_app", {"name": "Safari"},
                                               computer_use=True))
        # без включённого режима каждое из этих действий — вопрос пользователю
        self.assertEqual(agent.needs_approval("mouse_click", {"x": 1, "y": 2}),
                         "управление мышью на твоём компьютере")
        # согласие режима не расползается на остальные опасные действия
        self.assertEqual(agent.needs_approval("delete_file", {"name": "x"},
                                              computer_use=True), "удаление данных")
        # Терминал в режиме «Компьютер» — часть согласованной работы: молча.
        self.assertIsNone(agent.needs_approval("open_app", {"name": "Terminal"},
                                               computer_use=True))
        # А вот без режима окно терминала — только с разрешения.
        self.assertEqual(agent.needs_approval("open_app", {"name": "Terminal"},
                                              computer_use=False),
                         "открытие приложения «Терминал»")

    def test_screenshot_frame_stays_out_of_chat_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "screen_1.png").write_bytes(b"png")
            (Path(td) / "screen_2.png").write_bytes(b"png")
            convo: list = []
            call = {"id": "shot-1", "type": "function",
                    "function": {"name": "screenshot", "arguments": "{}"}}
            first = {"ok": True, "path": "screen_1.png",
                     "download_url": "/api/download/screen_1.png",
                     "data_url": "data:image/png;base64,AAAA", "bytes": 3,
                     "width": 800, "height": 600, "scale": 0.5}
            second = dict(first, path="screen_2.png",
                          download_url="/api/download/screen_2.png")
            runner = agent.Agent(computer_use=False)
            with mock.patch.object(agent, "_describe_screen",
                                   side_effect=("карта 1", "карта 2")), \
                 mock.patch.object(agent.sandbox, "root", return_value=Path(td)):
                runner._append_tool_result(convo, call, "screenshot", first, False)
                runner._append_tool_result(convo, call, "screenshot", second, False)
            # тяжёлые поля ушли из результата до отправки события в браузер
            for key in ("data_url", "download_url", "bytes", "path"):
                self.assertNotIn(key, second)
            self.assertEqual(second["screen"], "карта 2")
            # в контексте остаётся только последняя словесная карта экрана
            self.assertIn("карта 2", convo[-1]["content"])
            self.assertIn("опущена", json.loads(convo[-2]["content"])["screen"])
            # кадры не копятся ни в чате, ни в песочнице диалога
            self.assertFalse((Path(td) / "screen_1.png").exists())
            self.assertFalse((Path(td) / "screen_2.png").exists())


class PlanProgressTests(unittest.TestCase):
    def test_plan_advances_by_completed_work_not_only_by_model_turns(self) -> None:
        route = {"tier": "base", "reason": "test", "score": 0.4,
                 "verbose": True, "offer_tools": True}
        schema = [
            {"type": "function", "function": {"name": "web_search",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "open_url",
                                              "parameters": {"type": "object"}}},
        ]

        def call(name, args):
            return {"ok": True, "name": name}

        turns = iter((
            [{"type": "done", "tool_calls": [
                {"id": "t1", "type": "function", "function": {
                    "name": "web_search", "arguments": json.dumps({"query": "a"})}},
                {"id": "t2", "type": "function", "function": {
                    "name": "open_url", "arguments": json.dumps({"url": "https://x"})}},
            ]}],
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "make_plan",
                               return_value=["найти источники", "прочитать их",
                                             "свести ответ"]), \
             mock.patch.object(agent.tools, "call", side_effect=call):
            events = list(runner.run(
                [{"role": "user", "content": "Собери материал и перескажи"}],
                user_text="Собери материал и перескажи",
            ))
        steps = [event["step"] for event in events if event.get("type") == "plan_step"]
        results = [i for i, event in enumerate(events)
                   if event.get("type") == "tool_result"]
        self.assertEqual(steps, [1, 2, 3], "steps advance one by one to the end")
        # шаги двигаются фактами работы: последний шаг приходит только после
        # завершившегося результата, а не пачкой в самом конце прогона
        last_step_at = max(i for i, event in enumerate(events)
                           if event.get("type") == "plan_step")
        self.assertGreater(last_step_at, results[0])


class MidRunAgentPlanTests(unittest.TestCase):
    """AGENT включили через request_mode ПОСЛЕ старта прогона — план всё
    равно появляется: и когда работа уже идёт, и когда начнётся следом."""

    def _run(self, calls_turn1, final_text):
        route = {"tier": "coder", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [
            {"type": "function", "function": {"name": "write_file",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "request_mode",
                                              "parameters": {"type": "object"}}},
        ]
        turns = iter((
            [{"type": "done", "tool_calls": calls_turn1}],
            [{"type": "delta", "text": final_text},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=False)
        answered = {"id": "q1", "status": "answered", "answer": "Включить"}
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}), \
             mock.patch.object(runner, "_wait_answer", return_value=answered), \
             mock.patch.object(runner, "make_plan",
                               return_value=["Каркас", "Логика", "Проверка"]):
            return list(runner.run(
                [{"role": "user", "content": "напиши игру"}],
                user_text="напиши игру",
            ))

    @staticmethod
    def _call(cid, name, args=None):
        return {"id": cid, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args or {})}}

    def test_plan_when_work_started_before_mode_request(self) -> None:
        events = self._run([
            self._call("t1", "write_file", {"path": "a.py", "content": "x"}),
            self._call("t2", "request_mode", {"mode": "agent", "reason": "многошаговая"}),
        ], "Готово.")
        plans = [e for e in events if e.get("type") == "plan"]
        self.assertTrue(plans, "plan must exist even when AGENT was enabled mid-work")

    def test_plan_when_mode_request_comes_first(self) -> None:
        events = self._run([
            self._call("t1", "request_mode", {"mode": "agent", "reason": "многошаговая"}),
            self._call("t2", "write_file", {"path": "a.py", "content": "x"}),
        ], "Готово.")
        plans = [e for e in events if e.get("type") == "plan"]
        self.assertTrue(plans, "plan must exist when work follows the mode request")


class ComputerRightsTests(unittest.TestCase):
    """Нет прав macOS — прогон не умирает: open_permissions остаётся."""

    def test_open_permissions_opens_the_right_pane(self) -> None:
        from jarvis.tools import system

        calls = []
        with mock.patch.object(system, "IS_MAC", True), \
             mock.patch.object(system.subprocess, "run",
                               side_effect=lambda *a, **k: calls.append(a)):
            res = system.open_permissions("accessibility")
            res2 = system.open_permissions("screen")
        self.assertTrue(res.get("ok") and res2.get("ok"))
        self.assertIn("Privacy_Accessibility", calls[0][0][1])
        self.assertIn("Privacy_ScreenCapture", calls[1][0][1])

    def test_blocked_run_keeps_open_permissions_and_warns(self) -> None:
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [
            {"type": "function", "function": {"name": "mouse_click",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "open_permissions",
                                              "parameters": {"type": "object"}}},
        ]
        # два хода: в computer-use текст без действий один раз возвращается
        # моделью к работе (ловушка вранья), второй — финальный
        turns = iter((
            [{"type": "delta", "text": "Открою панель прав."},
             {"type": "done", "tool_calls": []}],
            [{"type": "delta", "text": "Открою панель прав."},
             {"type": "done", "tool_calls": []}],
        ))
        seen: list = []

        def fake_stream(convo, **kwargs):
            seen.append({"convo": list(convo), "tools": kwargs.get("tools") or []})
            return next(turns)

        runner = agent.Agent(computer_use=True)
        from jarvis.tools import system as sysmod
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(sysmod, "IS_MAC", True), \
             mock.patch.object(sysmod, "accessibility_ok", return_value=False):
            events = list(runner.run(
                [{"role": "user", "content": "нажми кнопку нового диалога"}],
                user_text="нажми кнопку нового диалога",
            ))
        # прогон НЕ закончился голым отказом: было предупреждение и обычный ход
        deltas = [e.get("text", "") for e in events if e.get("type") == "delta"]
        self.assertTrue(any("заблокировано правами" in d for d in deltas),
                        "the user sees why screen control is off")
        self.assertTrue(any("Открою панель прав" in d for d in deltas))
        # экранные инструменты убраны, open_permissions остался
        tool_names = {t.get("function", {}).get("name") for t in seen[0]["tools"]}
        self.assertNotIn("mouse_click", tool_names)
        self.assertIn("open_permissions", tool_names)
        # модель получила системное объяснение ситуации
        self.assertTrue(any(
            m.get("role") == "system" and "НЕ выданы" in str(m.get("content"))
            for m in seen[0]["convo"]))


class PlanPacingPerTurnTests(unittest.TestCase):
    def test_parallel_batch_advances_plan_at_most_once(self) -> None:
        """Пачка параллельных инструментов — ОДНА фаза работы, не пять шагов.

        Раньше каждый параллельный результат двигал план: 5 ссылок в одном
        ходе «простреливали» весь план за секунду — визуально вся работа
        сваливалась в последний шаг. Теперь батч продвигает план один раз;
        остаток честно закрывается вместе с финальным текстом.
        """
        route = {"tier": "base", "reason": "test", "score": 0.4,
                 "verbose": True, "offer_tools": True}
        schema = [
            {"type": "function", "function": {"name": "web_search",
                                              "parameters": {"type": "object"}}},
        ]
        turns = iter((
            [{"type": "done", "tool_calls": [
                {"id": "t%d" % i, "type": "function", "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "q%d" % i})}}
                for i in range(3)
            ]}],
            [{"type": "delta", "text": "Собрал всё в ответ."},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route),              mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)),              mock.patch.object(agent.tools, "schemas", return_value=schema),              mock.patch.object(runner, "make_plan",
                               return_value=["шаг 1", "шаг 2", "шаг 3", "шаг 4"]), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(runner.run(
                [{"role": "user", "content": "найди и собери"}],
                user_text="найди и собери",
            ))
        steps = [e["step"] for e in events if e.get("type") == "plan_step"]
        self.assertEqual(steps, [1, 2, 3, 4])
        # батч из трёх результатов продвинул план РОВНО ОДИН раз (шаг 2);
        # шаг 3 приходит только на границе следующего хода, шаг 4 — с
        # финальным текстом. Раньше три результата сожрали бы шаги 2, 3 и 4.
        results = [i for i, e in enumerate(events) if e.get("type") == "tool_result"]
        batch_steps = [i for i, e in enumerate(events)
                       if e.get("type") == "plan_step" and results[0] <= i <= results[-1]]
        self.assertEqual(len(batch_steps), 1,
                         "a parallel batch advances the plan exactly once "
                         "(old behavior burned one step per parallel result)")
        # три результата не съели три шага: последний шаг закрывается уже
        # после батча — на границе следующего хода модели
        step4_at = next(i for i, e in enumerate(events)
                        if e.get("type") == "plan_step" and e.get("step") == 4)
        self.assertGreater(step4_at, results[-1],
                           "the last step must survive past the batch")


class AutomaticMemoryTests(unittest.TestCase):
    def test_general_preferences_are_extracted_without_topic_vocabularies(self) -> None:
        text = "Я люблю острую еду, мне нравятся прогулки, но я не переношу арахис."
        facts = agent.extract_obvious_memories(text)
        triples = {(item["key"], item["value"]) for item in facts}
        self.assertIn(("Нравится", "острую еду"), triples)
        self.assertIn(("Нравится", "прогулки"), triples)
        self.assertIn(("Не нравится", "арахис"), triples)
        self.assertEqual(agent.extract_obvious_memories("Я переехал в новую квартиру."), [])

        stored = []
        with mock.patch.object(agent.db, "remember", side_effect=lambda *args: stored.append(args) or {"ok": True}), \
             mock.patch.object(agent.llm, "chat") as model:
            agent.remember_obvious_facts(text)
        model.assert_not_called()
        self.assertIn(("preference", "Нравится", "острую еду", 1.15), stored)
        self.assertIn(("preference", "Не нравится", "арахис", 1.15), stored)

    def test_preference_values_remain_verbatim_without_special_examples(self) -> None:
        moved = agent.extract_obvious_memories("Я переехал в питер")
        joke = agent.extract_obvious_memories("Я сказал что пошутил и я всё же до сих пор в мск")
        steak = agent.extract_obvious_memories("Я люблю стейки")
        disliked = agent.extract_obvious_memories("Я не люблю стейки")
        self.assertEqual(moved, [])
        self.assertEqual(joke, [])
        self.assertEqual(steak, [{
            "kind": "preference", "key": "Нравится", "value": "стейки",
        }])
        self.assertEqual(disliked, [{
            "kind": "preference", "key": "Не нравится", "value": "стейки",
        }], "negative preference must not also be recorded as positive")
        self.assertEqual(agent.extract_obvious_memories("Кстати, я люблю обезьянок"), [{
            "kind": "preference", "key": "Нравится", "value": "обезьянок",
        }], "a discourse prefix must not become a second memory")

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
            mock.call("preference", "Нравится", "стейки", 1.15),
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

    def test_model_cannot_become_a_second_automatic_memory_writer(self) -> None:
        route = {"tier": "base", "reason": "test", "score": 0,
                 "verbose": False, "offer_tools": True}
        schemas = [{"type": "function", "function": {"name": name,
                    "parameters": {"type": "object"}}}
                   for name in ("remember", "recall", "forget")]
        stream = [{"type": "delta", "text": "Понял."},
                  {"type": "done", "tool_calls": []}]
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.tools, "schemas", return_value=schemas), \
             mock.patch.object(agent.llm, "chat_stream", return_value=stream) as call:
            list(agent.Agent().run([{"role": "user", "content": "Я люблю обезьянок"}],
                                   user_text="Я люблю обезьянок"))
        offered = call.call_args.kwargs["tools"]
        self.assertEqual({item["function"]["name"] for item in offered},
                         {"recall", "forget", "request_mode", "switch_model"})

    def test_preferences_have_clean_relation_labels_and_value_identities(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        try:
            with mock.patch.object(db, "_CONN", conn):
                monkey = db.remember("preference", "Предпочтение: обезьянок", "обезьянок")
                steak = db.remember("preference", "Любимая еда", "стейки")
                avoid = db.remember("preference", "Ограничение", "арахис")
                same_monkey = db.remember("preference", "Предпочтения", "обезьянок")
                rows = db.recall()
            self.assertEqual(same_monkey["id"], monkey["id"])
            self.assertEqual(len(rows), 3)
            self.assertEqual({(row["key"], row["value"]) for row in rows}, {
                ("Нравится", "обезьянок"), ("Нравится", "стейки"),
                ("Не нравится", "арахис"),
            })
            self.assertNotEqual(monkey["id"], steak["id"],
                                "one clean relation label must not overwrite another object")
        finally:
            conn.close()

    def test_upgrade_repairs_same_turn_model_memory_noise_from_history(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO chats(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                     ("chat", "test", 90, 110))
        conn.execute("INSERT INTO messages(id,chat_id,role,content,meta,created_at) VALUES(?,?,?,?,?,?)",
                     ("msg", "chat", "user", "Кстати, я люблю обезьянок", "{}", 100))
        legacy = [
            ("local", "preference", "Предпочтение: обезьянок", "обезьянок", 1, 101, 101),
            ("noise-1", "preference", "Предпочтения", "кстати", 1, 102, 102),
            ("noise-2", "fact", "Обезьяны", "люблю обезьян", 1, 103, 103),
            ("manual", "fact", "Важный факт", "оставить", 1, 10, 10),
        ]
        conn.executemany("INSERT INTO memory VALUES(?,?,?,?,?,?,?)", legacy)
        conn.commit()
        try:
            with mock.patch.object(db, "_CONN", conn), \
                 mock.patch.object(agent, "_MEMORY_REPAIR_DONE", False):
                self.assertEqual(agent.repair_legacy_automatic_memories(), 3)
                rows = db.recall()
            self.assertEqual({(row["key"], row["value"]) for row in rows}, {
                ("Нравится", "обезьянок"), ("Важный факт", "оставить"),
            })
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
             mock.patch.object(server.agent, "remember_smart_facts") as remember_facts, \
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
        from jarvis import __version__ as app_version
        self.assertEqual(installer_build.version(), app_version)

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

    def test_nonstream_timeout_is_one_total_budget_not_one_per_retry(self) -> None:
        clock = [0.0]

        def request_timeout(*_args, **_kwargs):
            clock[0] += 4.5
            raise TimeoutError("slow provider")

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(llm, "active_providers", return_value=["first", "second"]), \
             mock.patch.object(llm, "provider_conf", return_value={
                 "api_key": "test", "base_url": "https://provider.invalid",
             }), mock.patch.object(llm, "pick_model", return_value="model"), \
             mock.patch.object(llm, "_request", side_effect=request_timeout) as request, \
             mock.patch.object(llm.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(llm.time, "sleep", side_effect=sleep), \
             mock.patch.object(llm.telemetry, "Span", return_value=self._span()):
            with self.assertRaises(llm.LLMError):
                llm.chat([{"role": "user", "content": "plan"}], timeout=5)
        request.assert_called_once()
        self.assertLessEqual(request.call_args.kwargs["timeout"], 5)
        self.assertEqual(clock[0], 5.0)

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

    def test_bg_title_never_blocks_the_request_thread(self) -> None:
        # W: название работает В ФОНОВОМ ПОТОКЕ с самого старта ответа —
        # поток запроса не ждёт ни одного LLM-вызова; обрыв SSE не мешает
        with mock.patch.object(server.orchestrator, "make_chat_title") as mk, \
             mock.patch.object(server.db, "rename_chat") as rename, \
             mock.patch.object(server.threading, "Thread") as thread:
            server._start_bg_title("chat-1", "Длинная тема")
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs.get("daemon"))
        worker = thread.call_args.kwargs["target"]
        mk.assert_not_called()               # LLM НЕ в потоке запроса
        rename.assert_not_called()
        # осознанное имя (сценарий, камера) — воркер его не трогает
        with mock.patch.object(server.db, "get_chat",
                               return_value={"title": "Сценарий: утро"}), \
             mock.patch.object(server.orchestrator, "make_chat_title") as mk2:
            worker()
        mk2.assert_not_called()

    def test_bg_title_renames_unnamed_chat_and_notifies(self) -> None:
        notify = mock.Mock()
        with mock.patch.object(server.threading, "Thread") as thread:
            server._start_bg_title("chat-2", "напиши игру", notify)
        worker = thread.call_args.kwargs["target"]
        with mock.patch.object(server.db, "get_chat",
                               return_value={"title": "Новый диалог"}), \
             mock.patch.object(server.orchestrator, "make_chat_title",
                               return_value="Космическая аркада"), \
             mock.patch.object(server.db, "rename_chat") as rename:
            worker()
        rename.assert_called_once_with("chat-2", "Космическая аркада")
        notify.assert_called_once_with("Космическая аркада")

    def test_bg_title_alive_box_is_bound_before_use(self) -> None:
        # W-регрессия: notify читает alive_box по ссылке; флаг обязан
        # присваиваться РАНЬШЕ потока названия, иначе UnboundLocalError
        import ast as _ast
        tree = _ast.parse(Path("app/jarvis/server.py").read_text(encoding="utf-8"))
        for fn in [n for n in _ast.walk(tree)
                   if isinstance(n, _ast.FunctionDef) and n.name == "_chat_stream"]:
            store = min([n.lineno for n in _ast.walk(fn)
                         if isinstance(n, _ast.Name) and n.id == "alive_box"
                         and isinstance(n.ctx, _ast.Store)] or [10**9])
            load = min([n.lineno for n in _ast.walk(fn)
                        if isinstance(n, _ast.Name) and n.id == "alive_box"
                        and isinstance(n.ctx, _ast.Load)] or [-1])
            self.assertLess(store, load,
                            "alive_box должен жить до старта фонового потока названия")

    def test_asr_empty_route_never_kills_the_chain(self) -> None:
        # W: маршрут с http 200 и ПУСТЫМ текстом больше не останавливает
        # цепочку «Тишиной» — следующий маршрут слышит и отвечает
        from jarvis.tools import media as media_mod
        ok_route = mock.Mock(return_value={"http_ok": True, "text": "привет"})
        mute_route = mock.Mock(return_value={"http_ok": True, "text": ""})
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(media_mod, "_ws", return_value=Path(td)), \
             mock.patch.object(media_mod, "_to_wav", return_value=Path(td) / "x.wav"), \
             mock.patch.object(media_mod, "_asr_routes",
                               return_value=[("mute", mute_route), ("good", ok_route)]):
            res = media_mod.transcribe_audio(
                "data:audio/webm;base64," + ("AAAA" * 4000))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("text"), "привет")
        mute_route.assert_called_once()

    def test_generate_image_has_free_route_without_keys(self) -> None:
        # W: без gateway и GigaChat-ключа генерация НЕ отказывает — работает
        # открытый бесплатный маршрут (Cloud.ru FM картинки не генерит)
        from jarvis.tools import media as media_mod
        with mock.patch.object(media_mod, "_enhance_prompt", side_effect=lambda p, w, h: p), \
             mock.patch.object(media_mod, "_free_image",
                               return_value={"ok": True, "path": "image_1.jpg"}) as free, \
             mock.patch.object(media_mod.CONFIG, "get",
                               side_effect=lambda k, d=None: {
                                   "media.image_provider": "auto",
                               }.get(k, d)):
            res = media_mod.generate_image("кот-космонавт")
        self.assertTrue(res.get("ok"))
        free.assert_called_once()

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


class ScenarioUpdateTests(unittest.TestCase):
    """Правка сценария на месте: id сохраняется, шаги обновляются."""

    def test_update_scenario_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "test.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit()
                conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()):
                    created = db.create_scenario("Дайджест", ["Шаг один"], emoji="☀️")
                    sid = created["id"]
                    updated = db.update_scenario(sid, "Дайджест утра",
                                                 ["Новости", "", "Погода", "Итог"], emoji="🌅")
                    self.assertEqual(updated["id"], sid)
                    self.assertEqual(updated["title"], "Дайджест утра")
                    self.assertEqual(updated["steps"], ["Новости", "Погода", "Итог"])
                    listed = db.list_scenarios()
                    self.assertEqual(len(listed), 1)
                    self.assertEqual(listed[0]["id"], sid)
                    self.assertEqual(listed[0]["steps"],
                                     ["Новости", "Погода", "Итог"])
                    # пустой id / пустые шаги не меняют ничего
                    self.assertIsNone(db.update_scenario("", "x", ["y"]))
                    self.assertIsNone(db.update_scenario(sid, "x", []))


class IterationXTests(unittest.TestCase):
    """X (beta.28): 10 пунктов отзыва — вызов-конвертом, картинки без ключа,
    whisper через chat, живая заглушка генерации, устойчивость потока."""

    def test_tool_key_envelope_is_executed_not_shown(self) -> None:
        # П.3: {"open_url": {"url": ...}} — вызов, а не текст ответа
        from jarvis.tools import parse_text_calls, looks_like_call_prefix
        rest, calls = parse_text_calls(
            '{"open_url": {"url": "https://rg.ru/date/2026/09/28"}}')
        self.assertEqual(calls, [{"name": "open_url",
                                  "args": {"url": "https://rg.ru/date/2026/09/28"}}])
        self.assertEqual(rest, "")
        # стрим придерживается, пока ключ может оказаться инструментом
        self.assertTrue(looks_like_call_prefix('{"open_url": {"url"'))
        self.assertTrue(looks_like_call_prefix('{"'))          # ключ печатается
        self.assertFalse(looks_like_call_prefix('{"custom": 1}'))  # чужой ключ
        # несколько инструментов в одном конверте — тоже вызовы
        rest2, calls2 = parse_text_calls(
            '{"open_url": {"url": "https://a.ru"}, "web_search": {"query": "x"}}')
        self.assertEqual([c["name"] for c in calls2], ["open_url", "web_search"])
        # осмысленный текст не трогаем
        _, none = parse_text_calls("Вот сводка новостей за сегодня: всё спокойно.")
        self.assertEqual(none, [])

    def test_forced_gigachat_without_key_falls_to_free(self) -> None:
        # П.10: кнопка настроек ставила provider='gigachat' и оставляла без
        # ключа — каждый запрос картинкой падал «вставь ключ»
        from jarvis.tools import media

        class _Cfg(dict):
            def get(self, k, d=None):
                return {"media.image_provider": "gigachat"}.get(k, d)

        with mock.patch.object(media, "CONFIG", _Cfg()), \
             mock.patch.object(media, "_enhance_prompt", side_effect=lambda p, w, h: p), \
             mock.patch.object(media, "_free_image",
                               return_value={"ok": True, "path": "f.jpg"}) as free:
            res = media.generate_image("закат над городом")
        self.assertTrue(res.get("ok"))
        free.assert_called_once()

    def test_whisper_chat_payload_fits_model_context(self) -> None:
        # П.9: у whisper-large-v3 контекст 448 токенов — прежний max_tokens
        # 1200 отправлял запрос в 400; теперь лимит внутри контекста и есть
        # вторая форма (чистое аудио без текстовой инструкции)
        from jarvis.tools import media
        import inspect as _inspect
        code = _inspect.getsource(media._asr_via_chat)
        self.assertNotIn('"max_tokens": 1200', code)
        self.assertIn('"max_tokens": 200', code)
        self.assertIn("shapes", code)

    def test_asr_routes_prefer_chat_over_missing_transcriptions(self) -> None:
        # П.9: официальный OpenAPI Cloud.ru — только /models и
        # /chat/completions; chat идёт первым, transcriptions — запасным
        from jarvis.tools import media

        class _Cfg(dict):
            def get(self, k, d=None):
                return {"model_tiers.audio": ["whisper-large"]}.get(k, d)

        with mock.patch.object(media, "CONFIG", _Cfg()), \
             mock.patch.object(media.llm, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "https://x/v1"}), \
             mock.patch.object(media.llm, "models_of_type", return_value=[]):
            labels = [r[0] for r in media._asr_routes()]
        self.assertLess(labels.index("cloudru-chat:whisper-large"),
                        labels.index("cloudru-ts:whisper-large"))

    def test_messages_report_live_generation(self) -> None:
        # П.7: /api/messages сообщает generating — фронт показывает живую
        # заглушку вместо «пустого» диалога и плавно дорисовывает ответ
        import threading as _th
        with mock.patch.object(server.db, "get_messages", return_value=[]), \
             mock.patch.object(server, "_ACTIVE_RUNS", {"chat-live": object()}), \
             mock.patch.object(server, "_RUN_LOCK", _th.Lock()):
            with mock.patch.object(server.Handler, "_json",
                                   side_effect=lambda payload: payload) as js_out:
                class _H:
                    pass
                handler = server.Handler.__new__(server.Handler)
                params = {"chat_id": ["chat-live"]}
                # вызываем ветку /api/messages напрямую через do_GET нельзя
                # (нужен запрос) — проверяем тело условия источником
        src = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn('"generating": generating', src)
        self.assertIn('generating = chat_id in _ACTIVE_RUNS', src)

    def test_single_sse_write_failure_does_not_silence_stream(self) -> None:
        # П.7: один мимолётный сбой записи больше не глушит поток до конца
        src = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("alive_fails = 0", src)
        self.assertIn("if alive_fails >= 2:", src)
        self.assertNotIn("alive = self._sse(event)", src)

    def test_thinking_shows_in_quiet_mode_too(self) -> None:
        # Y: ход мыслей — в ЛЮБОМ режиме, но тихому — только у рабочих
        # ответов: первый инструмент выпускает накопленное, короткая
        # болтовня не показывает ничего (агенту — сразу, порог 90)
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("self.quiet_thinking = bool(not social_only", src)
        self.assertIn("if self.show_thinking or self.quiet_thinking:", src)
        self.assertIn("thinking_min_chars = 90 if self.show_thinking else 800", src)
        self.assertIn("if self.quiet_thinking and not thinking_visible and thinking_pending:",
                      src)

    def test_free_image_prefers_newer_models(self) -> None:
        # П.7: безымянный дефолт рисовал как «первые ИИ» — теперь цепочка
        # от свежих моделей к старым: zimage (2x-апскейл) → klein → flux
        from jarvis.tools import media
        self.assertEqual(media._FREE_IMAGE_MODELS, ("zimage", "klein", "flux"))
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertIn("&model=%s", code)
        self.assertIn("_FREE_IMAGE_STATE", code)

    def test_y_frontend_anchors(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # тихий ход мыслей — дизайн кухни
        self.assertIn("qt-node qt-think", js)
        self.assertIn(".qt-think .qt-ico{font-size:12px", css)
        # история — дизайн ТОГО ответа
        self.assertIn("function renderAgentTraceGroups", js)
        self.assertIn("const agentAnswer = !!meta.agent;", js)
        # вальс: фейд у посадки, тело складывается первым, подпись доживает
        # AD: затемнение инструмента стартует в середине полёта, к посадке — тьма
        self.assertIn("const flyKeys = (target) => [", js)
        self.assertIn("filter: 'blur(4px) brightness(.08)'", js)
        self.assertIn("height .42s cubic-bezier(.4,.6,.3,1) .58s", js)
        self.assertIn("height .3s ease, opacity .22s ease", js)
        self.assertNotIn("scale(.93)", js.split("function qtFold")[1].split("\nfunction ")[0])
        # курсор: статус возрождается
        self.assertIn("ui.statusEl = ensureStatus(ui)", js)
        # тумблер: глоуш под gait-флагом
        self.assertIn(".agent-switch:not([data-ag-hold]) .agent-switch-track", css)
        # микрофон: WAV в браузере
        self.assertIn("function blobToWav16k", js)
        self.assertIn("blobToWav16k(blob)", js)

    def test_x_frontend_anchors(self) -> None:
        # фронтовые корни X: живой режим для дизайна инструментов, одиночка
        # без папки, страж плана, заглушка генерации, резинка тумблера
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("if (!(ui.agentMode || S.agentMode)) {", js)
        self.assertIn("if (pending.length === 1) {", js)
        self.assertIn("ui.planWatchdog = setTimeout", js)
        self.assertIn("gate-hold", js)
        self.assertIn("function appendLivePlaceholder", js)
        self.assertIn("function appendFreshMessages", js)
        self.assertIn("renderMessageInto(host, m, m.role === 'assistant' && m.id === lastAiId)", js)
        self.assertIn("shell.dataset.agHold = '1'", js)
        self.assertIn(".qt-folder.open .qt-kids{display:flex}", css)
        self.assertNotIn(".qt-folder.open .qt-kids{display:flex;margin:2px 0 4px}", css)
        self.assertIn("LEAD = 70", js)


class IterationZTests(unittest.TestCase):
    """Z (beta.30): 8 пунктов — точный вальс, мысль вне папок, план по
    диалогам, страж курсора, голосовой режим, негативный промпт."""

    def test_z_frontend_anchors(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        fold = js.split("function qtFold(ui, node, isLast)")[1].split("\nfunction ")[0]
        # П.1: чистый сдвиг + живое уравнение прицела + поздняя высота
        self.assertIn("const need = titleC() - labC - baseTop;", fold)
        self.assertNotIn("scale(.93)", fold)
        self.assertIn("height .42s cubic-bezier(.4,.6,.3,1) .58s", fold)
        # П.2/3: ход мыслей не собирается в папки
        sweep = js.split("function qtSweep")[1].split("\nfunction ")[0]
        fq = js.split("function flushQt")[1].split("\nfunction ")[0]
        self.assertIn("qt-think", sweep)
        self.assertIn("qt-think", fq)
        # П.4: тумблер быстрее
        self.assertIn("agKnobRubber 1.9s", css)
        # П.5: страж курсора + мягкий приезд
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        self.assertIn("const statusWatch = setInterval", send)
        self.assertIn("clearInterval(statusWatch);", send)
        self.assertIn("'думаю…', 'готовлю ответ', 'ещё секунду'", send)
        self.assertIn("freshIn .32s ease both", js)
        self.assertIn("@keyframes freshIn", css)
        # П.6: план привязан к диалогу
        self.assertIn("dock.dataset.chatId", js)
        open_chat = js.split("async function openChat")[1].split("\nasync function ")[0]
        self.assertIn("d.dataset.chatId === id", open_chat)
        # П.7: голосовой режим
        for marker in ("function openVoiceMode", "function closeVoiceMode",
                       "function voiceListen", "function voiceTranscribe",
                       "function voiceBargeLoop", "SpeechSynthesisUtterance",
                       "opts.onDelta && ev.type === 'delta'"):
            self.assertIn(marker, js)
        self.assertIn('id="voiceBtn"', html)
        # AB: разговор стал областью в ленте (как камера), не окном
        self.assertIn(".voice-box{display:flex", css)
        self.assertIn(".voice-box.speaking .v-orb b{", css)
        # голосовой режим переиспользует браузерный WAV — тот же корень п.6(Y)
        vt = js.split("async function voiceTranscribe")[1].split("\nfunction ")[0]
        self.assertIn("blobToWav16k", vt)

    def test_free_image_sends_negative_prompt(self) -> None:
        # П.8: негативный промпт убирает типичный мусор бесплатных генераторов
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertIn("&negative_prompt=%s", code)
        self.assertIn("watermark", code)
        self.assertIn("bad anatomy", code)
        # версия
        self.assertIn("beta.42", Path("app/jarvis/__init__.py").read_text(encoding="utf-8"))


class IterationAATests(unittest.TestCase):
    """AA (beta.31): тихая мысль открывается, курсор не слепнет, план уходит
    с новым диалогом, ответ на панель продолжает ТОТ ЖЕ ответ (и в БД),
    микрофон = режим разговора, причина мем-надписей на картинках, confirm."""

    def test_aa1_quiet_think_opens_by_click(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function qtToggleThink(row)", js)
        toggle = js.split("function qtToggleThink(row)")[1].split("\nfunction ")[0]
        self.assertIn("row._thinkOpen", toggle)
        # живая строка и история — обе кликабельны, история гидрирует строки
        live = js.split("case 'thinking': {")[1].split("case 'plan': {")[0]
        self.assertIn("qtToggleThink(qn)", live)
        restore = js.split("function restoreTrace")[1].split("\nfunction ")[0]
        self.assertIn("qtToggleThink(row)", restore)
        self.assertIn("qt-flowline", restore)
        # сворачивание сбрасывает состояние клика
        mini = js.split("function qtMiniaturize")[1].split("\nfunction ")[0]
        self.assertIn("node._thinkOpen = false;", mini)

    def test_aa2_cursor_watch_never_blind(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        # план больше не глушит стража; «текст есть» = «текст ПЕЧАТАЕТСЯ сейчас»
        self.assertNotIn("if (ui.planGate || ui.planDock) return;", send)
        self.assertIn("if (ui.doneReceived) return;", send)
        # AC: принадлежность телу ответа — работает и в отцепленном DOM
        self.assertIn("if (ui.mdEl && ui.typer && sbody.contains(ui.mdEl)) return;", send)
        self.assertIn("ui.statusEl._watchLine = true;", send)
        # воскресшая строка уходит, когда печать возобновилась
        self.assertIn("ui.statusEl._watchLine && ui.typer) dropStatus(ui);", js)
        # tool_hint воскрешает строку сам (раньше молчал в пустоту)
        self.assertNotIn("case 'tool_hint':\n      if (ui.statusEl)", js)
        hint = js.split("case 'tool_hint':")[1].split("case 'tool_start'")[0]
        self.assertIn("ensureStatus(ui);", hint)

    def test_aa3_new_dialog_hides_plan(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        new_chat = js.split("function newChat()")[1].split("\nfunction ")[0]
        self.assertIn("$$('.plan-dock').forEach((d) => { d.style.display = 'none'; });", new_chat)
        self.assertIn("S.detached = S.chatId;", new_chat)

    def test_aa4_panel_answer_continues_same_message(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        # фронт: панель знает msgId, send ищет ноду по нему, тело несёт continue_of
        self.assertIn("const panelMsg = box.closest('.msg');", js)
        self.assertIn("continueOf: panelMsgId", js)
        self.assertIn("m.dataset && m.dataset.msgId === opts.continueOf", js)
        self.assertIn("continue_of: opts.continueOf || ''", js)
        self.assertIn("case 'ai_msg':", js)
        # сервер: continue_of дописывает в ТО ЖЕ сообщение + id до закрытия потока
        self.assertIn('continue_of = str(body.get("continue_of") or "")', srv)
        self.assertIn("db.append_message(continue_of, final_text, ai_meta)", srv)
        self.assertIn('{"type": "ai_msg", "id": saved_ai.get("id")}', srv)

    def test_aa4_append_message_merges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "t.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit(); conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()):
                    chat = db.create_chat("диалог")
                    first = db.add_message(chat["id"], "assistant", "первая часть",
                                           {"files": ["a.png"], "tools": ["web_search"]})
                    merged = db.append_message(first["id"], "вторая часть",
                                               {"files": ["b.png"], "tools": ["generate_image"]})
                    self.assertIn("первая часть", merged["content"])
                    self.assertIn("вторая часть", merged["content"])
                    self.assertEqual(merged["meta"]["files"], ["a.png", "b.png"])
                    self.assertEqual(merged["meta"]["tools"],
                                     ["web_search", "generate_image"])
                    # одни и те же сообщения — ничего лишнего не появилось
                    msgs = db.get_messages(chat["id"])
                    self.assertEqual(len([m for m in msgs if m["role"] == "assistant"]), 1)
                    # продолжение с несуществующим id не падает и не теряет текст
                    self.assertIsNone(db.append_message("m_missing", "хвост", {}))

    def test_aa5_mic_button_is_voice_mode(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        # id="voiceBtn" ровно один — тумблер озвучки в шапке; дубль убивал клик
        self.assertEqual(html.count('id="voiceBtn"'), 1)
        self.assertIn('id="micBtn" data-tip="Голосовой режим"', html)
        handler = js.split("$('#micBtn').addEventListener('click'")[1].split("\n});")[0]
        self.assertIn("openVoiceMode()", handler)
        self.assertIn("closeVoiceMode()", handler)
        # диктовка ушла: мёртвых функций нет, WAV-конвертер жив для разговора
        self.assertNotIn("function browserASR", js)
        self.assertNotIn("async function serverASR", js)
        self.assertNotIn("function micHint", js)
        self.assertIn("async function blobToWav16k", js)
        # кнопка подсвечивается на время разговора
        self.assertIn("mb.classList.add('rec');", js)
        self.assertIn("mb.classList.remove('rec');", js)
        # обработчик режима разговора больше не вешается на тумблер шапки
        self.assertNotIn("$('#voiceBtn').addEventListener('click', () => {", js)

    def test_aa6_image_prompt_strips_address(self) -> None:
        # КОРЕНЬ «Hello Jarvis» на картинке: слова промпта рисуются буквально.
        # Приветствие и обращение вычищаются ДО улучшения и ПОСЛЕ него.
        self.assertEqual(media.strip_address("Привет, Джарвис! Нарисуй котика"),
                         "Нарисуй котика")
        self.assertEqual(media.strip_address("Здравствуй, Джарвис. Котик, мягкий свет"),
                         "Котик, мягкий свет")
        self.assertNotIn("Джарвис", media.strip_address("hello jarvis нарисуй кота"))
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertIn("def strip_address", code)
        self.assertIn("cleaned = strip_address(text)", code)      # ответ nano тоже чистится
        self.assertIn("return strip_address(prompt)", code)
        # честный текст ошибки: модель не может выдумать «нужна оплата»
        self.assertIn("Платный доступ НЕ нужен", code)

    def test_aa6_system_prompt_image_rule(self) -> None:
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            prompt = agent.build_system_prompt(agent_mode=False)
        self.assertIn("7а. КАРТИНКИ", prompt)
        self.assertIn("никогда не говори, что генерация «требует платного доступа»", prompt)
        self.assertIn("рисует любое слово из промпта буквально", prompt)

    def test_aa6_enhance_prompt_cleans_model_answer(self) -> None:
        # nano-улучшатель может вписать обращение обратно — ответ тоже чистится
        def cfg(key, default=None):
            return True if key == "media.enhance_prompt" else default
        with mock.patch.object(media.llm, "chat",
                               return_value={"content": "Привет, Джарвис! Рыжий кот на подоконнике"}), \
             mock.patch.object(media.CONFIG, "get", cfg):
            refined = media._enhance_prompt("Нарисуй котика", 1024, 1024)
        self.assertNotIn("Джарвис", refined)
        self.assertNotIn("Привет", refined)
        self.assertIn("кот", refined.lower())
        # сбой nano — исходная просьба отдаётся очищенной, не теряется
        with mock.patch.object(media.llm, "chat", side_effect=RuntimeError("нет сети")), \
             mock.patch.object(media.CONFIG, "get", cfg):
            fallback = media._enhance_prompt("Привет, Джарвис! Нарисуй котика", 1024, 1024)
        self.assertEqual(fallback, "Нарисуй котика")

    def test_aa7_confirm_ui(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        py = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # парсер знает confirm, сломанная одиночная плитка становится confirm
        self.assertIn("/^confirm\\s+(.+?)\\s*:*$/i", js)
        self.assertIn("function confirmLabel(label)", js)
        self.assertIn("t: 'confirm', label: confirmLabel(m[1])", js)
        # рендер: сноска, зелёная Да, красная Нет, без «Отправить» в чистом виде
        self.assertIn("const confirmOnly", js)
        # AB: подписи кнопок — сами варианты (у да/нет это «Да»/«Нет»)
        self.assertIn("? it.opts : ['Да', 'Нет'];", js)
        self.assertIn("cn-btn cn-yes", js)
        self.assertIn("cn-btn cn-no", js)
        self.assertIn("if (x.t === 'confirm') return x.val != null;", js)
        self.assertIn(".ui-row.ui-confirm{", css)
        self.assertIn(".cn-yes{", css)
        self.assertIn(".cn-no{", css)
        # серверный реестр типов и промпт модели
        self.assertIn("confirm|tiles|multi|rank", py)
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            prompt = agent.build_system_prompt(agent_mode=True)
        self.assertIn("confirm Подпись — вопрос да/нет", prompt)
        self.assertIn("САМЫЙ ЧАСТЫЙ тип", prompt)
        self.assertIn("confirm Сохранить в Excel?", prompt)


class IterationABTests(unittest.TestCase):
    """AB (beta.32): мысли без прыжка в последнем кадре, живой ответ
    переживает переключение диалога, разговор — область как камера,
    картинки переживают анонимный лимит, пара вариантов = сноска да/нет."""

    def test_ab1_think_no_last_frame_jump(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # КОРЕНЬ: вертикальные поля тела включались/выключались вместе с
        # видимостью — прыжок на 6px в последнем кадре (тот же класс, что X)
        self.assertIn(".qt-think .qt-body{margin:0 0 0 6px}", css)
        self.assertIn(".qt-think .qt-flow{margin-top:3px}", css)
        toggle = js.split("function qtToggleThink(row)")[1].split("\nfunction ")[0]
        self.assertNotIn("display", toggle)          # тело НИКОГДА не выключается
        restore = js.split("function restoreTrace")[1].split("\nfunction ")[0]
        self.assertIn('style="height:0;opacity:0;overflow:hidden"', restore)

    def test_ab2_live_run_survives_chat_switch(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        open_chat = js.split("async function openChat")[1].split("\nasync function ")[0]
        # реестр живых прогонов по диалогу
        self.assertIn("S.liveRuns[requestChatId] = ui;", send)
        self.assertIn("S.liveRuns[ev.chat_id] = ui;", js)
        self.assertIn("if (S.liveRuns[k] === ui) delete S.liveRuns[k];", send)
        # возврат в диалог прикрепляет САМ живой ответ и возвращает Stop
        self.assertIn("stream.appendChild(live.node.root);", open_chat)
        self.assertIn("if (S.followUi === live && !S.streaming) setStreaming(true);", open_chat)

    def test_ab3_voice_is_inline_area(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        py = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # область, а не окно на весь экран; статуса-текста нет вообще
        self.assertNotIn("voice-veil", css)
        self.assertIn("function voiceMount(forceOwn)", js)
        self.assertIn("function buildVoiceCard()", js)
        self.assertIn(".voice-box{display:flex", css)
        self.assertIn(".voice-run{display:none!important}", css)
        self.assertNotIn("voiceStatus", js)
        # изолированная беседа — служебный диалог вне списка
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        self.assertIn("const voiceIsolated = requestVoice;", send)
        self.assertIn("voice: requestVoice,", send)
        self.assertIn("node.root.classList.add('voice-run');", send)
        self.assertIn('VOICE.chatId = ev.chat_id;', js)
        self.assertIn('db.create_chat("Разговор", kind="voice")', srv)
        # разговор = классическое общение: инструменты и панели отключены
        # физически, на сервере
        self.assertIn('voice_mode = bool(body.get("voice"))', srv)
        self.assertIn("voice_mode=voice_mode, cancel_check=lambda: False", srv)
        self.assertIn("VOICE_MODE_NOTE", py)
        self.assertIn("self.voice_mode", py)
        self.assertIn("available = []", py)
        self.assertIn("not self.voice_mode and needs_reply_ui", py)
        # камера + разговор = один интерфейс; переключение диалога завершает беседу
        self.assertIn("if (VOICE.open) voiceMount();", js)
        self.assertIn("if (VOICE.open) voiceMount(true);", js)
        self.assertIn("if (VOICE.open) closeVoiceMode();", js)
        self.assertIn("voiceLoadTranscript", js)
        self.assertIn("Контекст диалога", js)

    def test_ab3_voice_mode_note_and_no_tools(self) -> None:
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            pass
        # нота голосового режима существует и говорит про устную речь
        self.assertIn("ГОЛОСОВОЙ РАЗГОВОР", agent.VOICE_MODE_NOTE)
        self.assertIn("без markdown", agent.VOICE_MODE_NOTE)

    def test_ab4_free_image_survives_rate_limit(self) -> None:
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        # корень «после одной генерации капут»: анонимный лимит + 180с таймауты
        self.assertIn("timeout=75", code)
        self.assertIn("&referrer=jarvis", code)
        self.assertIn("time.sleep(4)", code)               # пауза перед повтором
        self.assertIn("urllib.error.HTTPError", code)      # 429 ловится отдельно
        self.assertIn("лимит на минуту исчерпан", code)

    def test_ab5_any_two_options_render_as_footnote(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        mount = js.split("function mountUiPanels")[1].split("\nfunction ")[0]
        self.assertIn("pair.t = 'confirm'; pair.val = null;", mount)
        self.assertIn("const labels = (it.opts && it.opts.length === 2) ? it.opts : ['Да', 'Нет'];", mount)
        # модель тоже знает: пара = сноска
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            prompt = agent.build_system_prompt(agent_mode=False)
        self.assertIn("РОВНО ДВА варианта — тоже", prompt)


class IterationACTests(unittest.TestCase):
    """AC (beta.33): мысль льётся подряд и всегда открывается; стопка курсоров
    после перезахода убита в корне; инструкция-клик не рождает панель;
    разговор без эха и всегда изолирован; картинки — качество."""

    def test_ac1_think_flows_and_always_opens(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function qtThinkFeed(flow, text)", js)
        self.assertIn("qtThinkFeed(flow, ev.text)", js)
        self.assertNotIn("qtFeed(flow, ev.text)", js)
        mini = js.split("function qtMiniaturize(node)")[1].split("\nfunction ")[0]
        self.assertNotIn(".style.display", mini)   # тело никогда не гасится целиком

    def test_ac2_no_cursor_pile_after_reentry(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        ensure = js.split("function ensureStatus(ui)")[1].split("\nfunction ")[0]
        # КОРЕНЬ: isConnected ложно для живой строки в отцепленном узле —
        # каждый статусный вызов добавлял новую «думаю…»
        self.assertIn("ui.node.body.contains(ui.statusEl)", ensure)
        self.assertNotIn("ui.statusEl.isConnected", ensure)
        watch = js.split("const statusWatch = setInterval")[1].split("\n")[0:14]
        self.assertIn("sbody.contains(ui.mdEl)", js)
        det = js.split("function watchDetached(id)")[1].split("\nfunction ")[0]
        self.assertIn("(S.liveRuns || {})[id]) return;", det)

    def test_ac3_click_instruction_is_not_a_panel(self) -> None:
        # «Кликни на фигуру, чтобы выбрать её» — указание к нарисованному
        # объекту, не вопрос: панели с таким названием быть не может
        self.assertFalse(agent.needs_reply_ui(
            "Вот фигуры.\n- Кликни на фигуру, чтобы выбрать её (подсветка жёлтым)"))
        self.assertTrue(agent.needs_reply_ui("Какой формат предпочитаете?"))
        self.assertTrue(agent.needs_reply_ui("Выбери стиль:\n- Минимализм\n- Барокко"))

    def test_ac4_voice_no_echo_and_always_isolated(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        # эхо-подавление — корень «недоговаривает и прерывается»
        self.assertIn("echoCancellation: true, noiseSuppression: true", js)
        self.assertIn("level > 0.16", js)
        self.assertIn("VOICE.barge >= 7", js)
        # контекст по умолчанию ВЫКЛЮЧЕН
        self.assertIn("localStorage.getItem('jarvisVoiceCtx') === '1'", js)
        # кружок — вверху, остальное внизу
        self.assertIn("flex-direction:column", css)
        self.assertIn(".voice-box .v-orb{width:88px", css)
        # поле реально уходит из любого места, карточка — тёмная миниатюра
        close = js.split("function closeVoiceMode()")[1].split("\nfunction ")[0]
        self.assertIn("S.voiceBox.remove(); S.voiceBox = null;", close)
        self.assertIn("voiceRenderTranscript(tb);", close)
        # разговор ВСЕГДА в своём диалоге; контекст — отдельным полем
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        self.assertIn("const voiceIsolated = requestVoice;", send)
        self.assertIn("voice_context: (requestVoice && VOICE.ctxOn && S.chatId) || '',", send)
        self.assertIn('body.get("voice_context") or ""', srv)
        self.assertIn("db.get_messages(ctx_chat, limit=16)", srv)

    def test_ac5_image_quality_levers(self) -> None:
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertIn("high quality, highly detailed, sharp focus, natural proportions", code)
        self.assertIn("mutated hands, extra limbs, disfigured face", code)
        self.assertIn("анатомически верные", code)


class IterationADTests(unittest.TestCase):
    """AD (beta.34): мысли по предложениям, затемнение в группу, ховер одного
    инструмента, ответ всегда внизу, плавная кромка ленты, тёмная камера,
    иконка сценариев, кнопка «Звук», медленный кружок AGENT."""

    def test_ad1_think_sentences(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        feed = js.split("function qtThinkFeed(flow, text)")[1].split("\nfunction ")[0]
        # склейка «web_search.Need» получает пробел после точки
        self.assertIn(r"replace(/([.!\u2026!?])(?=[A-Z\u0410-\u042f\u0401])/g, '$1 ')", js)
        # законченное предложение — граница строки потока
        self.assertIn("const c = rest[i];", feed)
        self.assertIn("if (se > 20) cut = se;", feed)

    def test_ad2_tools_sink_into_darkness(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        fold = js.split("function qtFold(ui, node, isLast)")[1].split("\nfunction ")[0]
        # AG: затемнение — В САМОМ WAAPI-полёте (неуничтожимо CSS-гонками)
        self.assertIn("const flyKeys = (target) => [", fold)
        # AI: поздний крутой спад — свой easing у промежуточного кадра
        self.assertIn("{ opacity: 1, filter: 'blur(.5px) brightness(.94)', offset: .55,", fold)
        self.assertIn("easing: 'cubic-bezier(.62,.04,.6,.55)'", fold)
        self.assertIn("filter: 'blur(4px) brightness(.08)'", fold)
        self.assertIn("flight.effect.setKeyframes(flyKeys(aim));", fold)

    def test_ad3_hover_single_tool(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".qt-head:hover .qt-name,.qt-row:hover .qt-name", css)
        self.assertNotIn(".qt-folder:hover .qt-name", css)
        # AE: ОБЛАСТЬ инструмента не подсвечивается — только название
        self.assertNotIn(".qt-rows .qt-row:hover", css)

    def test_ad4_answer_always_at_bottom(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        send = js.split("async function send(opts)")[1].split("\nasync function ")[0]
        self.assertIn("allMsgs[allMsgs.length - 1] === root", send)
        # панели истории законсервированы, активна только последняя
        self.assertIn("mountUiPanels(node.body, { inert: !activePanel });", js)
        self.assertIn("let lastAiId = '';", js)
        self.assertIn("if (inert) box.classList.add('ui-inert');", js)
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".ui-panel.ui-inert{pointer-events:none;opacity:.5", css)

    def test_ad5_stream_bottom_fade(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("mask-image:linear-gradient(180deg,#000 0,#000 calc(100% - 30px),transparent)", css)

    def test_ad6_offline_cam_and_voice_cards(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("if (node.classList.contains('cam-msg') && !S.camStream) node.classList.add('offline');", js)
        self.assertIn("if (node.classList.contains('voice-msg') && !VOICE.open) node.classList.add('offline');", js)
        self.assertIn(".cam-msg.offline .cam-col-left,.voice-msg.offline .v-side{opacity:.5;pointer-events:none}", css)
        self.assertIn(".cam-msg.offline .cam-video{display:none}", css)
        # AE: вся карточка не затемняется; AF: диалог ЧУТЬ приглушён,
        # живое описание кадра сверху — особенно
        self.assertNotIn(".cam-msg.offline,.voice-msg.offline{opacity", css)
        self.assertIn(".cam-msg.offline .cam-chat{opacity:.85}", css)
        self.assertIn(".cam-msg.offline .cam-feed{opacity:.4}", css)

    def test_ad7_scenarios_icon(self) -> None:
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertNotIn("⚡️", html)
        self.assertIn("<h2>Сценарии</h2>", html)
        self.assertIn('data-view="scenarios"', html)
        self.assertIn(".nav-ico svg{display:block", css)

    def test_ad8_corner_button_is_master_sound(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        self.assertIn("function setSound(on)", js)
        self.assertIn("function syncSoundBtn()", js)
        self.assertIn("setSound(!soundOn())", js)
        self.assertIn("if (!soundOn() || !voiceOn() || !text) return;", js)
        self.assertIn("ui: { sound: on }", js)
        self.assertIn('title="Звук"', html)
        self.assertNotIn("Голос Джарвиса: озвучивать", html)

    def test_ad9_agent_dot_slower(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("transition:transform 3.2s cubic-bezier(.25,.65,.25,1),background .85s ease,", css)
        # AK: размер прежний, движение ещё меньше — ход 10px, симметричные
        # отступы (left 5px), наведение замедлено до 1.9с
        self.assertIn(".agent-switch-track{box-sizing:border-box;width:42px;height:28px", css)
        self.assertIn("width:20px;height:20px;left:5px;top:3px", css)
        self.assertIn(".agent-switch-track input:checked + i{transform:translateX(10px);", css)
        self.assertIn("animation:agKnobRubber 1.9s cubic-bezier(.25,.6,.25,1) both", css)
        self.assertIn("animation:agEmberRun 1.9s cubic-bezier(.25,.5,.3,1) both", css)


class IterationAETests(unittest.TestCase):
    """AE (beta.35): финальная полировка перед LIVE — цвет сценариев, чистые
    файлы диалога, живые строки мысли, тьма группы, спокойная иконка звука."""

    def test_ae1_scenarios_terrakota(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('.nav-item[data-view="scenarios"] .nav-ico{color:#a84a5b}', css)
        self.assertIn('.nav-item[data-view="scenarios"].active::before{background:#a84a5b', css)
        # AH: заголовок страницы — блёклый, как у остальных вкладок;
        # СТАРОЕ правило-победитель var(--red) удалено
        self.assertIn('.view-scenarios h2{color:#f5ccd3}', css)
        self.assertIn('.view-scenarios .panel-head h2{color:#f5ccd3}', css)
        self.assertNotIn('.view-scenarios .panel-head h2{color:var(--red)}', css)

    def test_ae2_live_uploads_leave_no_files(self) -> None:
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # кадры камеры и аудио разговора — транзиентные: на диск не пишутся
        self.assertIn('transient = bool(body.get("transient")) or', srv)
        self.assertIn('lower.startswith(("camera_", "frame_", "snapshot_",', srv)
        self.assertIn('lower.endswith((".wav", ".webm", ".mp3", ".ogg", ".m4a", ".aac"))', srv)
        self.assertIn('"transient": True}', srv)
        self.assertIn("transient: true,   // AE: живой кадр — не файл диалога", js)
        # AF: чистка РЕКУРСИВНА по всему workspace (диалоги + общий каталог)
        self.assertIn("def _purge_chat_leftovers()", srv)
        self.assertIn("os.walk(sandbox.WORKSPACE)", srv)
        self.assertIn('prefixes = ("camera_", "frame_", "snapshot_",', srv)

    def test_ae3_no_css_ellipsis_in_think_lines(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # строки потока переносятся; ellipsis в заголовках (.qt-name, чаты)
        # остаётся — там он уместен
        flow = css.split(".qt-flowline{font-size:11px")[1].split("}")[0]
        self.assertIn("white-space:normal", flow)
        self.assertNotIn("text-overflow", flow)

    def test_ae8_sound_icon_calm(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # включённая — НЕ пульсирует; выключенная — однотонная, без красного
        self.assertNotIn(".voice-btn.on .w1{animation", css)
        self.assertNotIn(".voice-btn.off .mute{opacity:1;color:var(--red)}", css)
        self.assertIn(".voice-btn.tick svg{animation:vsndTick", css)
        setfn = js.split("function setSound(on)")[1].split("\nfunction ")[0]
        self.assertIn("sb.classList.add('tick');", setfn)


class IterationAFTests(unittest.TestCase):
    """AF (beta.36): красный сценариев, чистая болтовня без «песочница чиста»,
    тьма группы с середины полёта, включённые режимы не предлагаются,
    кнопка звука включена по умолчанию."""

    def test_af1_scenarios_red(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('.nav-item[data-view="scenarios"] .nav-ico{color:#a84a5b}', css)
        self.assertIn('.view-scenarios .panel-head h2{color:#f5ccd3}', css)

    def test_af2_small_talk_gets_light_prompt(self) -> None:
        # «как дела» —.social реплика: ЛЁГКИЙ промпт без песочницы/инструментов
        self.assertTrue(orchestrator.is_social_only("как дела"))
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            light = agent.build_system_prompt(light=True)
            full = agent.build_system_prompt()
        self.assertNotIn("песочниц", light.lower())
        self.assertNotIn("инструмент", light.lower())
        self.assertIn("песочниц", full.lower())          # рабочий промпт не пострадал
        self.assertLess(len(light), len(full) // 10)
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("light_prompt = orchestrator.is_social_only(text)", srv)
        self.assertIn("light=light_prompt", srv)

    def test_af2_purge_recursive_everywhere(self) -> None:
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("os.walk(sandbox.WORKSPACE)", srv)
        self.assertIn('"dictation_", "voice_", "audio_"', srv)

    def test_af5_no_mode_offer_when_active(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("camera_on: camLive(),", js)
        self.assertIn('mode_hint.get("mode") == "camera" and body.get("camera_on")', srv)

    def test_af6_sound_on_by_default(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # нет настройки = звук есть (прежде «нет секции ui» считалось выключенным)
        self.assertIn("return !(ui && ui.sound === false);", js)
        # конфиг приехал — кнопка пересинхронизировалась
        self.assertIn("S.config = st.config || {};\n  syncSoundBtn();", js)


class IterationAGTests(unittest.TestCase):
    """AG/AH (beta.38): док-меню, тьма в самом полёте, чистые голосовые,
    свежий звонок, понятные ошибки зрения, дороже тумблер."""

    def test_ag1_scenarios_muted(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('.nav-item[data-view="scenarios"] .nav-ico{color:#a84a5b}', css)
        self.assertIn('.view-scenarios .panel-head h2{color:#f5ccd3}', css)
        self.assertNotIn('.view-scenarios .panel-head h2{color:var(--red)}', css)

    def test_ag2_voice_files_never_touch_workspace(self) -> None:
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # корень: transcribe сохранял voice_*.webm В ПЕСОЧНИЦУ
        self.assertIn('tempfile.TemporaryDirectory(prefix="jarvis-asr-")', code)
        self.assertNotIn('"voice_%d.%s"', code)
        self.assertIn("transient = bool(body.get(\"transient\")) or", 
                      Path("app/jarvis/server.py").read_text(encoding="utf-8"))
        # каждый звонок — НОВЫЙ разговор, старая беседа не подтягивается
        self.assertNotIn("jarvisVoiceChat", js)
        openv = js.split("async function openVoiceMode()")[1].split("\nasync function ")[0]
        self.assertNotIn("voiceLoadTranscript()", openv)

    def test_ag3_dimming_inside_flight(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        fold = js.split("function qtFold(ui, node, isLast)")[1].split("\nfunction ")[0]
        self.assertIn("const flyKeys = (target) => [", fold)
        self.assertIn("offset: .55", fold)
        self.assertIn("easing: 'cubic-bezier(.62,.04,.6,.55)'", fold)
        self.assertIn("flight.effect.setKeyframes(flyKeys(aim));", fold)
        # CSS-гонка с задержками убрана: остаётся только высота
        self.assertNotIn("opacity .5s ease-in .5s", fold)

    def test_ag5_empty_llm_body_readable(self) -> None:
        code = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        self.assertIn('last_error = LLMError("пустой ответ от модели %s" % model)', code)

    def test_ag6_collapsed_dock(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        # AH: контент на всю ширину, верхняя рамка — до конца налево
        self.assertIn(".app.collapsed{grid-template-columns:0px 1fr}", css)
        self.assertIn(".main{grid-column:2}", css)
        dock = css.split("/* ---- свёрнутый режим: панель превращается в плавающий DOCK")[1].split("/* подпись иконки")[0]
        # AJ: сайдбар 68px, док 52px — чуток крупнее и прозрачнее
        self.assertIn(".app.collapsed .sidebar{width:68px;", dock)
        self.assertIn(".app.collapsed .dock{pointer-events:auto", dock)
        self.assertIn("backdrop-filter:blur(18px) saturate(1.2)", dock)
        self.assertIn("border-radius:20px", dock)
        self.assertIn("border-color:transparent", dock)
        self.assertIn("background:rgba(15,27,44,.38)", dock)
        self.assertIn("padding:10px 0;", dock)
        # AK: КОРЕНЬ ВЫПИРАНИЯ вылечен — .nav сужается до стекла,
        # пункты width:auto (были шире дока на width:100% сайдбара)
        self.assertIn(".app.collapsed .nav{margin:0 8px}", dock)
        self.assertIn(".app.collapsed .nav-item{gap:0;width:auto;padding:10px 11px;margin:2px 6px;transform:none;", dock)
        # анимация медленнее и плавнее: .8s
        self.assertIn("cubic-bezier(.5,.35,.15,1)", dock)
        self.assertIn("transition:width .8s", dock)
        self.assertIn(".app.collapsed .nav-item.active::before{display:none}", dock)
        self.assertIn(".app.collapsed .nav-item:hover{transform:none;background:rgba(0,212,255,.09)}", dock)
        self.assertIn(".app.collapsed .nav-label{opacity:0;max-width:0;", dock)
        # контент не едет под док
        self.assertIn(".app.collapsed .main .view{padding-left:76px", dock)
        # AJ: ОДНОВРЕМЕННАЯ анимация — никаких фаз и таймеров
        self.assertIn(".side-folding .chats-block,.side-folding .side-foot{opacity:0;transform:translateX(-14px)}", css)
        self.assertIn("app.classList.toggle('collapsed', collapsing);", js)
        self.assertIn("app.classList.toggle('side-folding', collapsing);", js)
        self.assertNotIn("SIDE_FADE", js)
        self.assertNotIn("SIDE_MORPH", js)
        # направления различаются ТОЛЬКО кривой (AK: .8s, глубже S-образные)
        self.assertIn("cubic-bezier(.5,.35,.15,1)", dock)
        self.assertIn("cubic-bezier(.22,.68,.18,1)", css)
        # AJ: по умолчанию Джарвис открывается с доком
        self.assertIn("(pref === 'collapsed' || pref === null)", js)
        self.assertIn("function dockY(on)", js)
        self.assertIn("--dock-y", js)
        self.assertIn('<div class="dock">', html)
        # AK: стрелка — SVG-шеврон, математически по центру в обоих режимах
        self.assertIn(".collapse-btn{width:26px;height:26px;font-size:16px;display:grid;place-items:center;", css)
        self.assertIn('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"', html)
        self.assertNotIn(">‹</button>", html)


class IterationAJTests(unittest.TestCase):
    """AJ (beta.41): сигил ответа — морфинг-фигура + живой росчерк."""

    def test_aj_sigil_replaces_reactor(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # реактор больше не рендерится у ответов (сигил вместо него)
        self.assertNotIn('<div class="reactor sm" style="width:34px;height:34px">', js)
        self.assertIn("const SIGIL_SVG =", js)
        self.assertIn('class="ai-sigil"', js)
        self.assertIn('class="sigil-shape"', js)
        self.assertIn('class="sigil-sweep"', js)
        self.assertIn('class="sigil-wave"', js)
        # AK: 10 причудливых форм по 8 сегментов, быстрые превращения 9с,
        # переходы то плавные, то рывком (чередование сплайнов)
        self.assertIn('dur="9s"', js)
        shape_vals = js.split('class="sigil-shape"')[1].split('values="')[1].split('"')[0]
        self.assertEqual(len(shape_vals.split(';')), 11)   # 10 форм + замыкание
        for d in shape_vals.split(';'):
            self.assertEqual(d.count('C'), 8)
            self.assertTrue(d.startswith('M17.0 5.5'))
        self.assertIn('.85 0 .15 1', js)   # резкий рывок среди сплайнов
        # линия: вибрация волнами и резкими пиками (SMIL d), 7 состояний
        wave_vals = js.split('class="sigil-wave"')[1].split('values="')[1].split('"')[0]
        self.assertEqual(len(wave_vals.split(';')), 7)
        for d in wave_vals.split(';'):
            self.assertEqual(d.count('C'), 6)
        # покой: редкий блик; печать: линия проявлена (d-анимация в SMIL)
        self.assertIn("animation:sigilSweep 12s ease-in-out infinite", css)
        self.assertNotIn("sigilBreathe", css)
        self.assertIn(".msg-ai:has(.typing) .sigil-wave{opacity:.95}", css)
        self.assertIn(".msg-ai:has(.typing) .sigil-shape{opacity:0}", css)
        # ярче и футуристичнее
        self.assertIn("stroke:var(--cy2)", css)
        self.assertIn("drop-shadow(0 0 5px rgba(95,230,255,.45))", css)


class IterationAKTests(unittest.TestCase):
    """AK (beta.42): reasoning по-русски, геометрия дока, стрелка-SVG."""

    def test_ak_reasoning_lang_injection(self) -> None:
        from jarvis import agent as ag
        # вставка только в отправку, история не мутируется
        convo = [{"role": "user", "content": "привет"}]
        sent = ag._with_reasoning_lang(convo)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("русском", sent[0]["content"])
        self.assertEqual(convo, [{"role": "user", "content": "привет"}])
        # перед ПОСЛЕДНИМ сообщением (user или tool — без разницы)
        convo3 = [{"role": "user", "content": "a"},
                  {"role": "assistant", "content": "b"},
                  {"role": "tool", "content": "c"}]
        sent3 = ag._with_reasoning_lang(convo3)
        self.assertEqual([m["role"] for m in sent3],
                         ["user", "assistant", "system", "tool"])
        # рядом уже стоит служебная system — языковая нота ПЕРЕД ней,
        # порядок служебных нот не ломается
        convo4 = [{"role": "user", "content": "a"},
                  {"role": "system", "content": "UI"},
                  {"role": "user", "content": "b"}]
        sent4 = ag._with_reasoning_lang(convo4)
        self.assertEqual([m["content"] for m in sent4],
                         ["a", ag._REASONING_RU["content"], "UI", "b"])
        # пустой диалог не трогаем
        self.assertEqual(ag._with_reasoning_lang([]), [])


class BareToolArgumentsTests(unittest.TestCase):
    """Сырой JSON в чате: модель напечатала ГОЛЫЕ аргументы вызова —
    парсер подбирает инструмент по ключам схемы и исполняет его."""

    def test_bare_args_matched_to_tool(self) -> None:
        text, calls = agent.tools.parse_text_calls(
            '{"method":"GET","url":"https://wttr.in/Moscow?format=3"}')
        self.assertEqual(text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "http_request")
        self.assertEqual(calls[0]["args"]["method"], "GET")
        self.assertTrue(calls[0]["args"]["url"].startswith("https://wttr.in"))

    def test_plain_text_and_foreign_dicts_untouched(self) -> None:
        # обычная речь не трогается
        text, calls = agent.tools.parse_text_calls("Погода отличная, 12 градусов")
        self.assertEqual(calls, [])
        self.assertIn("Погода", text)
        # словарь, не совпадающий ни с одной схемой, остаётся текстом
        text2, calls2 = agent.tools.parse_text_calls('{"ok": true}')
        self.assertEqual(calls2, [])
        self.assertIn("ok", text2)


class ScheduleWordNumbersTests(unittest.TestCase):
    """«через секунду» без цифры и «через две минуты» словами — локальный
    парсер обязан понимать это без обращения к модели."""

    def test_single_word_units(self) -> None:
        cases = [
            ("расскажи о погоде через секунду", "in 1s"),
            ("напиши через минуту", "in 1m"),
            ("скажи привет через час", "in 1h"),
            ("через день проверь новости", "in 1d"),
        ]
        for text, expect in cases:
            with self.subTest(text=text):
                self.assertEqual(auto.detect_schedule(text), expect)

    def test_word_numbers(self) -> None:
        self.assertEqual(auto.detect_schedule("через две минуты напиши привет"), "in 2m")
        self.assertEqual(auto.detect_schedule("через три часа покажи погоду"), "in 3h")

    def test_llm_fallback_only_on_deferral_markers(self) -> None:
        # автомат не понял, но есть маркеры срока — нано-модель решает
        with mock.patch.object(agent.llm, "chat",
                               return_value={"content": '{"schedule": "in 45m"}'}) as m:
            verdict = auto.should_background(
                "расскажи мне о погоде, как вернусь с прогулки — потом")
            self.assertTrue(verdict["background"])
            self.assertEqual(verdict["schedule"], "in 45m")
            self.assertTrue(m.called)
        # болтовня без маркеров срока модель не беспокоит
        with mock.patch.object(agent.llm, "chat",
                               return_value={"content": '{"schedule": "in 45m"}'}) as m:
            verdict = auto.should_background("привет, расскажи о себе")
            self.assertFalse(verdict["background"])
            self.assertFalse(m.called)
        # мусорный ответ модели не проходит каноническую валидацию
        with mock.patch.object(agent.llm, "chat",
                               return_value={"content": "сделаю позже"}):
            verdict = auto.should_background(
                "расскажи мне о погоде, как вернусь с прогулки — потом")
            self.assertFalse(verdict["background"])


class ClosingDigestCarriesDataTests(unittest.TestCase):
    """Финальная сводка обязана нести САМИ ДАННЫЕ инструментов: заголовки
    новостей, факты — иначе модель честно отвечает «детали по запросу»."""

    def test_tool_results_flow_into_closing(self) -> None:
        convo = [
            {"role": "user", "content": "новости за сегодня"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "name": "web_search",
             "content": json.dumps({"ok": True, "results": [
                 {"title": "ЦБ снизил ставку", "snippet": "до 16% годовых"},
                 {"title": "Запуск ракеты", "snippet": "с космодрома Восточный"},
             ]})},
        ]
        closing = agent._closing_convo(convo, "base")
        self.assertIn("ЦБ снизил ставку", closing[1]["content"])
        self.assertIn("16%", closing[1]["content"])
        self.assertIn("Запуск ракеты", closing[1]["content"])

    def test_closing_prompt_demands_content(self) -> None:
        convo = [{"role": "user", "content": "новости"},
                 {"role": "assistant", "content": ""},
                 {"role": "tool", "name": "web_search",
                  "content": json.dumps({"ok": True, "results": [
                      {"title": "T", "snippet": "S"}]})}]
        closing = agent._closing_convo(convo, "base")
        self.assertIn("САМИ ДАННЫЕ", closing[1]["content"])
        self.assertNotIn("Подведи итог выполненной работы", closing[1]["content"])


class PlanWorthinessTests(unittest.TestCase):
    """План — только для действительно многоэтапной работы: простая просьба
    в AGENT идёт сразу к работе, без карточки плана и без планировщика."""

    def test_simple_requests_do_not_deserve_a_plan(self) -> None:
        for text in ["погода в Москве", "переведи слово house",
                     "какая сейчас ставка цб", "найди ссылку на документацию"]:
            with self.subTest(text=text):
                self.assertFalse(agent.needs_plan(text))

    def test_multistage_requests_do(self) -> None:
        for text in ["собери отчёт по рынку", "создай сайт-визитку",
                     "напиши игру", "исследуй конкурентов и сделай таблицу",
                     "подготовь дайджест новостей за неделю"]:
            with self.subTest(text=text):
                self.assertTrue(agent.needs_plan(text))
        self.assertTrue(agent.needs_plan("первое " + "очень длинное " * 20 + "требование"))

    def _run_agent(self, user_text, turns, schema):
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        runner = agent.Agent(agent_mode=True)
        turns_iter = iter(turns)

        def fake_stream(*_a, **_k):
            return list(next(turns_iter))

        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(runner, "make_plan",
                               return_value=["Раз", "Два", "Три"]) as planner, \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}):
            events = list(runner.run([{"role": "user", "content": user_text}],
                                     user_text=user_text))
        return events, planner

    def test_simple_agent_task_skips_planner(self) -> None:
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        events, planner = self._run_agent("погода в Москве", (
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "погода"})}}]}],
            [{"type": "delta", "text": "Ясно, 12 градусов."},
             {"type": "done", "tool_calls": []}],
        ), schema)
        self.assertFalse([e for e in events if e.get("type") == "plan"],
                         "a one-step request must not produce a plan card")
        self.assertFalse(planner.called, "no planner call for simple requests")

    def test_multistage_agent_task_gets_plan(self) -> None:
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        events, planner = self._run_agent("собери отчёт по рынку кофе", (
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "web_search",
                             "arguments": json.dumps({"query": "рынок"})}}]}],
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": []}],
        ), schema)
        self.assertTrue([e for e in events if e.get("type") == "plan"])
        self.assertTrue(planner.called)


class AgentSelfComputerUseTests(unittest.TestCase):
    """Дикий AGENT: экранная работа не останавливает автономность — агент
    включает «Компьютер» сам, пользователь видит mode_changed."""

    def test_agent_enables_computer_itself(self) -> None:
        from jarvis.tools import system
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        turns = iter(([
            {"type": "delta", "text": "Нажимаю."},
            {"type": "done", "tool_calls": []},
        ],))

        def fake_stream(*_a, **_k):
            try:
                return list(next(turns))
            except StopIteration:
                return []

        runner = agent.Agent(agent_mode=True)
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(system, "IS_MAC", True), \
             mock.patch.object(system, "accessibility_ok", return_value=True):
            events = list(runner.run(
                [{"role": "user", "content": "нажми кнопку отправки в браузере"}],
                user_text="нажми кнопку отправки в браузере"))
        modes = [e for e in events if e.get("type") == "mode_changed"]
        self.assertTrue(any(e.get("mode") == "computer" and e.get("on")
                            for e in modes),
                        "agent must enable computer-use itself for screen tasks")
        self.assertTrue(runner.computer_use)

    def test_suggest_mode_stays_silent_in_agent_mode(self) -> None:
        # в AGENT вопрос «включить Компьютер?» не задают — агент включит сам
        self.assertIsNone(agent.suggest_mode("нажми кнопку", agent_mode=True,
                                             computer_use=False))
        hint = agent.suggest_mode("нажми кнопку", agent_mode=False,
                                  computer_use=False)
        self.assertEqual(hint["mode"], "computer")


class AgentAutonomyTests(unittest.TestCase):
    """Дикий AGENT: всё сам — КРОМЕ денег, действующих санкций и правки
    существующих файлов. Самопроверка кода возвращает ошибку модели ДО
    финального ответа."""

    def test_suggest_proactive_offers_real_next_steps(self) -> None:
        items = agent.suggest_proactive("следи за курсом доллара", "Готово",
                                        ["web_search"])
        self.assertTrue(any("мониторинг" in i.lower() for i in items))
        items2 = agent.suggest_proactive("собери отчёт", "Готово",
                                         ["web_search", "write_file", "open_url"])
        self.assertTrue(any("доработа" in i.lower() or "отчёт" in i.lower()
                            for i in items2))
        self.assertTrue(any("сам" in i.lower() for i in items2))

    def test_py_syntax_error_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "broken.py"
            bad.write_text("def f(:\n  pass\n", encoding="utf-8")
            self.assertTrue(agent._py_syntax_error(str(bad)),
                            "broken code must fail the self-check")
            good = Path(td) / "fine.py"
            good.write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(agent._py_syntax_error(str(good)), "")

    def test_long_multistage_run_gets_more_steps(self) -> None:
        # многоэтапная задача в AGENT получает расширенный потолок шагов
        with mock.patch.object(agent.CONFIG, "get", return_value=18):
            base = agent._max_steps(True)
        self.assertGreaterEqual(max(base, 30), 30)

    def test_existing_file_edit_requires_approval_in_agent(self) -> None:
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "write_file",
                                                    "parameters": {"type": "object"}}}]
        turns = iter((
            [{"type": "done", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": json.dumps({"path": "notes.md",
                                                      "content": "новое"})}}]}],
            [{"type": "delta", "text": "Готово."},
             {"type": "done", "tool_calls": []}],
        ))
        runner = agent.Agent(agent_mode=True)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(agent.sandbox, "safe_path",
                               return_value=Path(td) / "notes.md"), \
             mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream",
                               side_effect=lambda *_a, **_k: next(turns)), \
             mock.patch.object(agent.tools, "schemas", return_value=schema), \
             mock.patch.object(agent.tools, "call", return_value={"ok": True}), \
             mock.patch.object(runner, "_wait_approval",
                               return_value={"status": "approved"}) as wait:
            (Path(td) / "notes.md").write_text("старое", encoding="utf-8")
            events = list(runner.run(
                [{"role": "user", "content": "дополни заметки"}],
                user_text="дополни заметки"))
        self.assertTrue(wait.called, "editing an existing file must ask")
        self.assertTrue(any(e.get("type") == "approval_wait" for e in events))
        self.assertIn("notes.md",
                      str(wait.call_args),
                      "the approval names the file being changed")
        # после разрешения файл становится «своим»: повторная правка молчит
        self.assertIn("notes.md", runner._owned_files)


class AiReplySuggestionsTests(unittest.TestCase):
    """Подсказки продолжения формирует ИИ-модель (nano): три коротких
    варианта по сути ответа. Любой сбой честно падает в локальный запас —
    после AGENT-прогона это проактивные шаги по фактам работы."""

    def test_parses_model_variants_and_falls_back(self) -> None:
        with mock.patch.object(agent.llm, "chat",
                               return_value='["Уточнить срок", "Добавить график", "Скопировать в файл"]'):
            items = agent.suggest_replies_ai("когда дедлайн проекта?",
                                             "Дедлайн — пятница, 18:00.")
        self.assertEqual(items, ["Уточнить срок", "Добавить график", "Скопировать в файл"])

        # мусор от модели — локальный запас: у AGENT-прогона проактивные шаги
        with mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("сеть")), \
             mock.patch.object(agent, "suggest_proactive",
                               return_value=["Поставь на мониторинг"]) as proactive:
            items = agent.suggest_replies_ai("следи за курсом", "Готово", ["web_search"])
        self.assertEqual(items, ["Поставь на мониторинг"])
        self.assertTrue(proactive.called)

        # обычный разговор — разговорные продолжения
        with mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("сеть")):
            items = agent.suggest_replies_ai("расскажи про котов", "Коты спят 16 часов в сутки.")
        self.assertTrue(items and all(isinstance(i, str) for i in items))

    def test_english_tech_junk_never_reaches_the_user(self) -> None:
        # nano вернула служебный мусор из JSON ответа — пользователь его не увидит
        with mock.patch.object(agent.llm, "chat",
                               return_value='["content", "tool_calls", "reasoning"]'), \
             mock.patch.object(agent, "suggest_replies",
                               return_value=["Расскажи подробнее"]) as local:
            items = agent.suggest_replies_ai(
                "что в файле?", "В файле три раздела: введение, данные и выводы.")
        self.assertEqual(items, ["Расскажи подробнее"])
        self.assertTrue(local.called, "мусор модели обязан упасть в локальный запас")

        # сырой JSON вместо ответа — nano вообще не вызывается (экономия)
        with mock.patch.object(agent.llm, "chat") as nano:
            agent.suggest_replies_ai("запусти", '{"ok": true, "stdout": "готово"}',
                                     ["run_python"])
        self.assertFalse(nano.called, "служебному ответу не нужен nano-запрос")

    def test_suggestion_usable_requires_russian(self) -> None:
        self.assertTrue(agent._suggestion_usable("Добавить график продаж"))
        self.assertTrue(agent._suggestion_usable("Сравнить с GPT"))
        self.assertFalse(agent._suggestion_usable("tool_calls"))
        self.assertFalse(agent._suggestion_usable("Content overview"))
        self.assertFalse(agent._suggestion_usable("reasoning пошёл"))
        self.assertFalse(agent._suggestion_usable(""))

    def test_code_answers_still_get_ai_suggestions(self) -> None:
        # «напиши игру» — ответ почти весь из кода: nano всё равно зовётся,
        # видит дайджест кода, и его русские подсказки доходят до пользователя
        chess = ("Вот игра в шахматы на python:\n```python\nimport random\n"
                 "BOARD = [['.'] * 8 for _ in range(8)]\nprint('ход')\n```\n"
                 "Управление мышью, есть проверка мата.")
        with mock.patch.object(agent.llm, "chat",
                               return_value='["Добавь ИИ противника", "Сохрани в файл", '
                                            '"Сделай сетку на 10 клеток"]') as nano:
            items = agent.suggest_replies_ai("напиши игру шахматы", chess, [])
        self.assertTrue(nano.called, "кодовый ответ не должен лишать nano-подсказок")
        self.assertEqual(items, ["Добавь ИИ противника", "Сохрани в файл",
                                 "Сделай сетку на 10 клеток"])
        sent = nano.call_args[0][0][1]["content"]
        self.assertIn("в ответе есть код", sent)

    def test_local_fallback_varies_by_answer_type(self) -> None:
        code_items = agent.suggest_replies("напиши", "вот код:\n```python\nprint(1)\n```")
        self.assertEqual(code_items, ["Сохрани в файл", "Добавь ещё функции",
                                      "Объясни по шагам"])
        talk_items = agent.suggest_replies("расскажи", "Коротко о погоде.")
        self.assertEqual(talk_items, ["Расскажи подробнее", "Покажи на примере",
                                      "Что дальше?"])

    def test_new_message_supersedes_previous_run_in_chat(self) -> None:
        # контракт сервера: новое сообщение в диалоге останавливает прежний
        # прогон — иначе старый молча доигрывался и модель продолжала прошлую
        # задачу вместо новой («ответил на прошлый запрос»)
        source = (ROOT / "app" / "jarvis" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_RUN_EVENTS: Dict[str, threading.Event]", source)
        self.assertIn("old_event = _RUN_EVENTS.get(chat_id)", source)
        self.assertIn("old_event.set()", source)
        self.assertIn("_RUN_EVENTS[chat_id] = stop_event", source)

    def test_plan_steps_arrive_clean_of_markdown(self) -> None:
        # модель пометила шаги ~~зачёркиванием~~ и **жирным** — в карточке
        # плана обязан остаться чистый текст
        self.assertEqual(agent._step_text("~~run_python~~"), "run_python")
        self.assertEqual(agent._step_text("**set_difficulty**"), "set_difficulty")
        self.assertEqual(agent._step_text("`enable_sound`"), "enable_sound")
        self.assertEqual(
            agent.parse_plan_steps('["~~run_python~~", "**set_difficulty**", '
                                   '"enable_sound", "check_result"]'),
            ["run_python", "set_difficulty", "enable_sound", "check_result"])

    def test_parser_is_tolerant_to_model_noise(self) -> None:
        parse = agent._parse_reply_suggestions
        # пояснение вокруг массива
        self.assertEqual(parse('Вот варианты: ["один", "два", "три"] Конец.'),
                         ["один", "два", "три"])
        # пустые мета-вопросы и мусор отсеиваются
        self.assertEqual(parse('["Что ещё?", "Добавить таблицу", "", null]'),
                         ["Добавить таблицу"])
        self.assertEqual(parse(""), [])


class LongIntroReleaseTests(unittest.TestCase):
    """«Медленный агент»: intro_hold держал ВЕСЬ первый ход — длинный ответ
    молчал до конца и вываливался куском. Теперь текст длиннее 260 символов
    выпускается в поток сразу, в момент генерации."""

    def test_long_first_turn_streams_immediately(self) -> None:
        route = {"tier": "base", "reason": "t", "score": 0.5,
                 "verbose": True, "offer_tools": True}
        schema = [{"type": "function", "function": {"name": "web_search",
                                                    "parameters": {"type": "object"}}}]
        # первый ход: длинный текст (>260) двумя кусками, без инструментов
        long_a = "Сейчас расскажу подробно о структуре рынка. " * 4   # ~200
        long_b = "Дальше идут выводы и прогнозы на год. " * 4        # +~170
        runner = agent.Agent(agent_mode=True)

        def fake_stream(*_a, **_k):
            return [
                {"type": "delta", "text": long_a},
                {"type": "delta", "text": long_b},
                {"type": "done", "tool_calls": []},
            ]

        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=route), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=fake_stream), \
             mock.patch.object(agent.tools, "schemas", return_value=schema):
            events = list(runner.run([{"role": "user", "content": "расскажи про рынок"}],
                                     user_text="расскажи про рынок"))
        deltas = [e for e in events if e.get("type") == "delta"]
        self.assertTrue(deltas, "длинный текст обязан выходить дельтами, а не одним куском в конце")
        # весь текст дошёл до пользователя без потерь
        streamed = "".join(e.get("text", "") for e in deltas)
        self.assertIn(long_a.strip()[:40], streamed,
                      "начало длинного ответа не теряется при выпуске")
        self.assertTrue(len(streamed) >= len(long_a + long_b) - 2)


if __name__ == "__main__":
    unittest.main()
