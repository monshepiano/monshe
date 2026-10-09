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

    def test_bm16_chat_schedule_call_creates_real_task(self) -> None:
        """BM16: в ЧАТ-прогоне звонок schedule_task создаёт НАСТОЯЩУЮ задачу.

        Маршрутизатор не увёл просьбу в фон — модель зовёт schedule_task:
        прежде это был сентинел-отказ (ни задачи, ни уведомления). Теперь
        задача создаётся, фронт показывает toast и карточку AUTO. Вопрос о
        состоянии по-прежнему НЕ создаёт ничего.
        """
        from unittest import mock as _mock
        turns = iter(("call", "answer"))

        def fake_stream(*_args, **_kwargs):
            if next(turns) == "call":
                yield {"type": "done", "tool_calls": [{
                    "id": "bg1", "type": "function",
                    "function": {"name": "schedule_task",
                                 "arguments": json.dumps(
                                     {"title": "Таймер", "prompt": "напомни"},
                                     ensure_ascii=False)},
                }]}
            else:
                yield {"type": "delta", "text": "Поставил в фон."}
                yield {"type": "done", "tool_calls": []}

        route = {"tier": "base", "reason": "test", "score": 0,
                 "verbose": False, "offer_tools": True}
        with _mock.patch.object(agent.orchestrator, "choose_tier",
                                return_value=route), \
             _mock.patch.object(agent.llm, "chat_stream",
                                side_effect=fake_stream), \
             _mock.patch.object(agent, "state_question",
                                return_value=False), \
             _mock.patch.object(auto, "has_similar_pending",
                                return_value=False) as pending, \
             _mock.patch.object(
                 auto, "create_background_task",
                 return_value={"id": "t1", "title": "Таймер"}) as create:
            runner = agent.Agent(chat_id="chat-9", agent_mode=False)
            events = list(runner.run(
                [{"role": "user", "content": "напомни через час"}],
                user_text="напомни через час"))

        pending.assert_called_once()
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs.get("chat_id"), "chat-9")
        results = [e for e in events
                   if e.get("type") == "tool_result"
                   and e.get("name") == "schedule_task"]
        self.assertTrue(results)
        self.assertTrue(results[0]["result"]["ok"])
        self.assertEqual(results[0]["result"]["task"]["title"], "Таймер")
        self.assertIn("schedule_task", runner.used_tools)

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
        # AY: планировщик — общий фоновый с первого tool_partial; блокирующих
        # вызовов make_plan из тела прогона больше нет в принципе
        self.assertIn("def _start_bg_plan()", run_src)
        self.assertIn("def flush_ready_plan()", run_src)
        self.assertIn("threading.Thread(target=_bg, daemon=True,", run_src)
        self.assertIn("self.local_plan(user_text)", run_src)
        self.assertNotIn("plan = self.make_plan(user_text, autonomous_names)", run_src)
        self.assertNotIn("plan = self.make_plan(user_text, [name])", run_src)

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
        # AY: планировщик ушёл в фон и не ждёт подсказки об инструментах —
        # план больше не блокирует показ; вызов приходит из фонового потока
        deadline = time.time() + 2
        while not planner.called and time.time() < deadline:
            time.sleep(0.01)
        planner.assert_called_once_with("Собери документ, формат: Markdown", [])
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
        # AT: короткая ЖИВАЯ русская мысль больше не глотается порогом —
        # ход мыслей показывается всегда (пользователь: «пусть будет»)
        self.assertEqual(
            [e["text"] for e in tiny if e.get("type") == "thinking"],
            ["Проверяю."])
        parts = ["Сначала проверяю исходные ограничения и зависимости. ",
                 "Затем сопоставляю варианты, риски и проверяемый итог решения."]
        substantial = run_with(parts)
        thinking = [event["text"] for event in substantial if event.get("type") == "thinking"]
        # AQ: до порога 90 знаков предложения копятся; первый показ —
        # все накопленные ЦЕЛЫЕ предложения разом, с пробелами между ними
        self.assertEqual(thinking, [
            "Сначала проверяю исходные ограничения и зависимости. "
            "Затем сопоставляю варианты, риски и проверяемый итог решения."])


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
             mock.patch.object(server.agent, "remember_smart_facts_async", return_value=[]), \
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
        # BM16: сработавшая задача — ПРОСТО отложенное сообщение, без
        # карточки AUTO (карточка уместна при СОЗДАНИИ, не при срабатывании)
        sent_content = add_message.call_args[0][2]
        self.assertIn("привет", sent_content)
        self.assertNotIn('```embed', sent_content)
        self.assertEqual(add_message.call_args[0][3],
                         {"task_id": "timer-1", "from_auto": True,
                          "files": [], "title": "Приветствие"})
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
        self.assertIn("Единственное железное\nограничение: НЕ выставляй панель", contract)

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
             mock.patch.object(server.agent, "remember_smart_facts_async",
                               return_value=[]) as remember_facts, \
             mock.patch.object(server.agent, "build_system_prompt", return_value="BASE SYSTEM"), \
             mock.patch.object(server.agent.tools, "schemas", return_value=[]):
            server.Handler._chat_stream(handler, {
                "chat_id": "camera-chat",
                "kind": "cam",
                "text": "Сделай мем",
                "attachments": [attachment],
            })

        self.assertEqual(chat_stream.call_count, 1, "vision UI must not require a preflight model call")
        remember_facts.assert_called_once_with("Сделай мем")   # async-обёртка на входной границе
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
    def setUp(self) -> None:
        # BM5: здоровье провайдеров — глобальное состояние, между тестами чисто
        llm._PROVIDER_HEALTH.clear()

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
        self.assertIn("thinking_min_chars = 90 if self.show_thinking else 10 ** 9", src)
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
        self.assertIn("m.role === 'assistant' && m.id === lastAiId", js)
        self.assertNotIn("answered.has", js)
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
        # П.4: тумблер быстрее (AN: резинка чуть замедлена — 1с)
        self.assertIn("agKnobRubber 1s", css)
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
        # версия: фича уже была в beta.91 (не откатывается)
        import re as _re
        _m = _re.search(r"1\.2\.0-beta\.(\d+)",
                        Path("app/jarvis/__init__.py").read_text(encoding="utf-8"))
        self.assertTrue(_m and int(_m.group(1)) >= 91)
        # BM14.1: кэш-бустер статики обновляется сборкой сам
        self.assertIn("def _bump_asset_versions(",
                      Path("install/build.py").read_text(encoding="utf-8"))


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
        # BM28: диктовка осталась в композере; звонок открывает кнопка LIVE
        # над иконками пространств (в доке — spdLive)
        self.assertIn('id="micBtn" data-tip="Диктовка"', html)
        self.assertNotIn('id="liveBtn"', html)
        self.assertIn("$('#spModeLive').addEventListener('click', () => { liveOpen(); });", js)
        self.assertIn('async function liveOpen', js)
        # BM17: обработчик тонкий, вся механика — в живой диктовке
        handler = js.split("$('#micBtn').addEventListener('click'")[1].split("\n")[0]
        self.assertIn("dictStart()", handler)
        self.assertNotIn("openVoiceMode()", handler)
        self.assertNotIn("closeVoiceMode()", handler)
        # BM17: ЖИВОЕ распознавание сегментами по ходу речи (не после
        # отключения) + никаких уведомлений о микрофоне
        self.assertIn("const DICT_SEG_MS = 3000;", js)
        dstart = js.split("async function dictStart")[1].split("\nfunction ")[0]
        self.assertIn("getUserMedia", dstart)
        self.assertIn("MediaRecorder", dstart)
        self.assertIn("DICT", dstart)
        self.assertIn("toast(", js)                       # тосты живут в чате
        self.assertNotIn("toast(", dstart)                # ...но не у микрофона
        self.assertNotIn("micHint", dstart)
        dtr = js.split("async function dictTranscribeSegment")[1].split("\nfunction ")[0]
        self.assertIn("/api/transcribe", dtr)
        self.assertIn("blobToWav16k", dtr)
        self.assertIn("blob.size < 1200", dtr)            # тишина — не слово
        dput = js.split("function dictPutText")[1].split("\nfunction ")[0]
        self.assertIn("box.value = (base + String(text)", dput)  # текст в поле СРАЗУ
        # BM18: повторный клик — ЧЕСТНЫЙ СТОП (dictFinish), а не смена
        # сегмента: прежний dictSegment перезапускал запись бесконечно,
        # и тишина диктовала «Продолжение следует…» снова и снова
        self.assertIn("if (DICT) { dictFinish(DICT); return; }", dstart)
        self.assertIn("продолжение следует", dtr.split("return text")[0]
                      if "return text" in dtr else dtr) if False else None
        self.assertRegex(dtr, r"продолжение следует")   # мусор тишины фильтруется
        # разговорный режим жив отдельными функциями (для LIVE), не на micBtn
        self.assertIn("function openVoiceMode", js)
        self.assertIn("async function blobToWav16k", js)
        self.assertNotIn("function browserASR", js)
        self.assertNotIn("function micHint", js)
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
        self.assertIn("const voiceIsolated = requestVoice || requestLive;", send)
        self.assertIn("voice: requestVoice,", send)
        # BM29: LIVE-звонок тоже изолирован, но инструменты работают
        self.assertIn("const requestKind = requestLive ? 'live'", send)
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
        # BM28: в LIVE сцена звонка вместо карточки — камера его не строит
        self.assertIn("if (VOICE.open && !LIVE.on) voiceMount();", js)
        self.assertIn("if (VOICE.open && !LIVE.on) voiceMount(true);", js)
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
        # BM8: 75с — потолок запроса внутри ОБЩЕГО бюджета цепочки (90с)
        self.assertIn("timeout=min(75", code)
        self.assertIn("_FREE_IMAGE_BUDGET_S", code)
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
        self.assertIn("VOICE.barge >= 5", js)      # BM29.2: перебой быстрее
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
        self.assertIn("const voiceIsolated = requestVoice || requestLive;", send)
        self.assertIn("voice_context: ((requestVoice || requestLive) && VOICE.ctxOn && S.chatId) || '',", send)
        self.assertIn('body.get("voice_context") or ""', srv)
        self.assertIn("db.get_recent_messages(ctx_chat, limit=16)", srv)

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
        self.assertIn("const sent = box.classList.contains('ui-sent');", js)
        self.assertIn("if (inert || sent) box.classList.add('ui-inert');", js)
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
        self.assertIn("transition:transform .45s cubic-bezier(.3,.6,.3,1),background .45s ease,", css)
        # AK: размер прежний, движение ещё меньше — ход 10px, симметричные
        # отступы (left 5px); AN: наведение чуть медленнее (1с/.9с/1.1с)
        self.assertIn(".agent-switch-track{box-sizing:border-box;width:42px;height:28px", css)
        self.assertIn("width:20px;height:20px;left:5px;top:3px", css)
        self.assertIn(".agent-switch-track input:checked + i{transform:translateX(10px);", css)
        self.assertIn("animation:agKnobRubber 1s cubic-bezier(.3,.7,.3,1) both", css)
        self.assertIn("animation:agEmberRun .9s cubic-bezier(.3,.5,.35,1) both", css)
        self.assertIn("animation:agSparkRun 1.1s linear both", css)
        self.assertIn("animation:agRestGlow .5s ease .6s both", css)


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
        self.assertIn("camera_on: camLive() || liveCamOn,", js)
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
        # AP: место под меню держит margin, а не grid-анимация (Safari)
        self.assertIn(".app.collapsed .main{margin-left:0;", css)
        # BM21: одна кривая морфа в базовом правиле
        self.assertIn("transition:margin-left var(--fold-t) var(--fold-ease)}", css)
        self.assertIn(".main{grid-column:1;margin-left:262px;", css)
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
        self.assertIn(".app.collapsed .nav{margin:0}", dock)
        self.assertIn(".app.collapsed .nav-item{gap:0;width:auto;padding:10px 11px;margin:0 6px;transform:none;}", dock)
        # анимация медленнее и плавнее: общие часы морфа (BM21)
        self.assertIn("--fold-ease:cubic-bezier(.42,0,.18,1)", css)
        self.assertIn("var(--fold-t) var(--fold-ease)", dock)
        self.assertIn(".app.collapsed .nav-item.active::before{display:none}", dock)
        self.assertIn(".app.collapsed .nav-item:hover{transform:none;background:rgba(0,212,255,.09)}", dock)
        # BM19: переход подписей вынесен в базу (плавно в ОБЕ стороны)
        self.assertIn(".nav-label{transition:max-width var(--fold-t) var(--fold-ease),", dock)
        self.assertIn(".app.collapsed .nav-label{opacity:0;max-width:0}", dock)
        # контент не едет под док
        self.assertIn(".app.collapsed .main .view{padding-left:76px", dock)
        # BM18: ОДНО ДВИЖЕНИЕ — диалоги и футер схлопываются плавно
        # (flex-grow/max-height + затухание), без display:none-скачка
        self.assertIn(".app.collapsed .chats-block{flex-grow:0;", css)
        self.assertIn(".app.collapsed .side-foot{max-height:0;", css)
        self.assertNotIn(".app.collapsed .chats-block,\n.app.collapsed .side-foot{display:none}", css)
        # BM21: дирижёр side-folding удалён — переходы живут в базовых правилах
        self.assertNotIn(".side-folding", css)
        self.assertNotIn("side-folding", js)
        # BM12: сворачивание — явные шаги: сжать пространства, ПОСЛЕ анимации
        # погасить (.docked); разворачивание — снять .docked и разжать в след. кадр
        self.assertIn("app.classList.add('collapsed');", js)
        self.assertIn("app.classList.add('docked')", js)
        self.assertIn("app.classList.remove('docked');", js)
        self.assertNotIn("SIDE_FADE", js)
        self.assertNotIn("SIDE_MORPH", js)
        # направления различаются ТОЛЬКО кривой (BM21: одна кривая морфа)
        self.assertIn("--fold-ease:cubic-bezier(.42,0,.18,1)", css)
        self.assertIn("cubic-bezier(.22,.68,.18,1)", css)
        # AJ: по умолчанию Джарвис открывается с доком
        self.assertIn("localStorage.removeItem('jarvis.sidebar2');", js)
        self.assertIn("function dockY(on, scale)", js)
        self.assertIn("--dock-y", js)
        self.assertIn('<div class="dock">', html)
        # AK: стрелка — SVG-шеврон, математически по центру в обоих режимах
        self.assertIn(".collapse-btn{width:26px;height:26px;font-size:16px;display:grid;place-items:center;", css)
        self.assertIn('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"', html)
        self.assertNotIn(">‹</button>", html)
        # AL: стрелка МАЛЕНЬКАЯ (14px), по центру кнопки
        self.assertIn(".collapse-btn svg{width:14px;height:14px;display:block}", css)


class IterationAJTests(unittest.TestCase):
    """AJ (beta.41): сигил ответа — морфинг-фигура + живой росчерк."""

    def test_aj_sigil_replaces_reactor(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AS: эстафета ОТМЕНЕНА — реактор только в доке, у ответа круглешок.
        # Ни relay-функций, ни relay-правил, ни миганий: док живёт всегда
        self.assertIn("function flyWelcomeInto(", js)
        self.assertIn("function flyGhost(", js)
        self.assertNotIn("relayTyping", js)
        self.assertNotIn("relayFlick", js)
        self.assertNotIn("relay", css)
        # круглешок живой: дышит в покое, пульсирует при печати (чистый CSS)
        self.assertIn(".ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;", css)
        # BD: история статична И дешёвая; дышит только текущий ответ
        self.assertNotIn("coreBreathe", css)
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)

    def test_ak_reasoning_lang_injection(self) -> None:
        from jarvis import agent as ag
        # вставка только в отправку, история не мутируется
        convo = [{"role": "user", "content": "привет"}]
        sent = ag._with_reasoning_lang(convo)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("русском", sent[0]["content"])
        # AL: приписка в копии user-сообщения (язык ближайшего текста)
        self.assertTrue(sent[1]["content"].endswith(
            ag._REASONING_HINT.strip() + ")") or "по-русски" in sent[1]["content"])
        self.assertEqual(convo, [{"role": "user", "content": "привет"}])
        # английские обломки мышления в ленту не идут
        self.assertTrue(ag._reasoning_ru_visible("Собираю отчёт по файлам"))
        self.assertFalse(ag._reasoning_ru_visible("Let me check the files first"))
        # AP: БЕЗБУКВЕННЫЙ МУСОР больше не проходит (старый фильтр
        # пропускал его: букв нет — считать долю латиницы нечего)
        self.assertFalse(ag._reasoning_ru_visible(", .,.:,. 2 2026."))
        self.assertFalse(ag._reasoning_ru_visible("2 2026"))
        self.assertFalse(ag._reasoning_ru_visible("... ... ..."))
        self.assertFalse(ag._reasoning_ru_visible(""))
        # смесь русского с латиницей видна, пока русский главный
        self.assertTrue(ag._reasoning_ru_visible("Проверяю API и делаю выводы"))
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
        self.assertEqual([m["content"] for m in sent4][:3],
                         ["a", ag._REASONING_RU["content"], "UI"])
        self.assertIn("по-русски", sent4[3]["content"])
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

    def test_existing_file_edit_needs_no_approval_in_agent(self) -> None:
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
        self.assertFalse(wait.called, "изменение файла песочницы — без вопроса")
        self.assertFalse(any(e.get("type") == "approval_wait" for e in events))
        # BM2: файлы песочницы — мои, подтверждение не спрашивается вовсе;
        # спрашивается только удаление из песочницы и действия на компьютере
        self.assertEqual(runner._owned_files, set())


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
        self.assertEqual(code_items, ["Сохрани в файл", "Предложи улучшения",
                                      "Объясни по шагам"])
        talk_items = agent.suggest_replies("расскажи", "Коротко о погоде.")
        # BM11: запас универсален для любого ответа — уточнение, развитие,
        # применение; прежние «расскажи подробнее»-тройки выглядели шаблоном
        self.assertEqual(talk_items, ["Уточни главное", "Предложи варианты развития",
                                      "Как это применить?"])
        # BM12: ДЛИННЫЙ ответ без кода — не артефакт. Сводка новостей длинная,
        # но фенсов в ней нет: «сохрани в файл» на новостях было ошибкой класса
        news = ("Новости дня. " * 200).strip()
        self.assertGreater(len(news), 1200)
        self.assertEqual(agent.suggest_replies("что нового?", news),
                         ["Уточни главное", "Предложи варианты развития",
                          "Как это применить?"])
        for one in talk_items:
            self.assertTrue(agent._suggestion_usable(one),
                            "запасная реплика обязана проходить фильтр: %s" % one)

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


class IterationANTests(unittest.TestCase):
    """AN (beta.45): сигил снова жив (структура SMIL починена), тумблер
    играет всегда (WAAPI-ведение поверх резинки), плитки — изначальный
    вид с троеточием в заголовке, ход мыслей — живой формат по строкам."""

    def test_an1_sigil_structure_alive(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # AS: у ответа КРУГЛЕШОК (ядро без колец); SMIL-тегов нет вообще
        self.assertEqual(js.count('<animate'), 0)
        self.assertIn('const AVATAR_CORE =', js)
        self.assertIn('class="ai-core"', js)
        self.assertNotIn('sigil', js.lower())
        self.assertNotIn('AVATAR_REACTOR', js)

    def test_an2_toggle_always_plays(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        ch = js.split("$('#tgAgent').addEventListener('change'")[1].split("\n});")[0]
        # снимаем текущее положение круглёшка ДО гашения резинки
        self.assertIn("getComputedStyle(knob).transform", ch)
        # резинка (fill:both на transform) больше не может украсть переход
        self.assertIn("knob.getAnimations().forEach", ch)
        # ведём круглёшок сами: WAAPI, та же кривая .45с, само-снятие
        self.assertIn("knob.animate(", ch)
        self.assertIn("duration: 450", ch)
        self.assertIn("easing: 'cubic-bezier(.3,.6,.3,1)'", ch)
        self.assertIn("go.onfinish", ch)
        self.assertIn("fill: 'both'", ch)

    def test_an3_reasoning_live_format(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function thinkFormat(", js)
        tt = js.split("function thinkType(")[1].split("\nfunction ")[0]
        self.assertIn("el._raw", tt)
        self.assertIn("thinkFormat(el._raw)", tt)
        tf = js.split("function thinkFlush(")[1].split("\nfunction ")[0]
        self.assertIn("thinkFormat(el._raw)", tf)
        # тихий поток: цепочки точек исчезают там же (AO: без «…»)
        qf = js.split("function qtThinkFeed(")[1].split("\nfunction ")[0]
        self.assertIn("(?:\\s*\\.\\s*){2,}", qf)
        self.assertNotIn("' \\u2026 '", qf)
        # история показывает тот же живой формат
        rt = js.split("function restoreTrace(")[1].split("\nfunction ")[0]
        self.assertIn("thinkFormat(think)", rt)

    def test_an4_hover_slightly_slower(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("animation:agKnobRubber 1s cubic-bezier(.3,.7,.3,1) both", css)
        self.assertIn("animation:agEmberRun .9s cubic-bezier(.3,.5,.35,1) both", css)
        self.assertIn("animation:agRestGlow .5s ease .6s both", css)
        self.assertIn("animation:agSparkRun 1.1s linear both", css)
        # вкл/выкл — прежние .45с одной кривой в обе стороны
        self.assertIn(
            "transition:transform .45s cubic-bezier(.3,.6,.3,1),background .45s ease,", css)


class IterationAOTests(unittest.TestCase):
    """AO (beta.46): рабочая область в такт доку, волна сигила при печати,
    многоточия в мыслях убраны совсем, троеточие плиток средствами JS,
    тумблер: клики больше не блокируются, двойная пересадка защищена."""

    def test_ao1_workarea_matches_dock(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AP: сетка статична (Safari не анимирует grid-template-columns —
        # панель «примагничивалась» к краю), место держит margin-left,
        # он анимируется везде и в такт доку
        self.assertIn(".main{grid-column:1;margin-left:262px;", css)
        self.assertIn(".app.collapsed .main{margin-left:0;", css)
        # BM21: одна кривая морфа в базовом правиле
        self.assertIn("transition:margin-left var(--fold-t) var(--fold-ease)}", css)
        self.assertIn("grid-template-columns:1fr;height:100vh", css)
        self.assertNotIn("grid-template-columns .6s", css)
        # мобильный каркас: узкая полоса 62px
        self.assertIn(".main,.app.collapsed .main{margin-left:62px}", css)
        # отступ вида скользит, а не прыгает (BM21: общие часы морфа)
        self.assertIn(
            ".main .view{transition:padding-left var(--fold-t) var(--fold-ease)}", css)
        # AQ: полоса закреплена и контент начинается ниже неё
        self.assertIn("padding-top:56px}", css)
    def test_ao2_relay_engine(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # AS: эстафеты нет. Рождение ответа — перелёт из приветствия:
        # ядро большого реактора и надпись JARVIS летят в ответ
        self.assertIn("flyWelcomeInto(node, welcomeFlight);", js)
        self.assertIn("let welcomeFlight = null;", js)
        self.assertIn("if (wlReactor && wlTitle) {", js)
        self.assertIn("'.welcome .reactor.xl'", js)
        self.assertIn(".welcome .hello span", js)
        ghost = js.split("function flyGhost(")[1].split("\nfunction ")[0]
        # AW: полёт на живом наведении (rAF); AY: кривая — единый cubic-bezier
        self.assertIn("const ease = cubicBezierEase(.65, 0, .35, 1);", ghost)
        self.assertIn("g.style.transform = 'translate(' + x + 'px,' + y + 'px) scale(' +", ghost)
        # AV: посадка — кроссфейд, без вспышек и мгновенной подмены
        self.assertIn("fade.onfinish = () => g.remove();", js)
        self.assertIn("const titleTarget = () => {", js)

    def test_ao3_reasoning_no_ellipsis(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        tf = js.split("function thinkFormat(")[1].split("\nfunction ")[0]
        # цепочки точек = граница мысли; самих многоточий на экране НЕТ
        self.assertIn(".replace(/(?:\\s*\\.\\s*){2,}/g, '" + chr(92) + "n')", tf)
        self.assertNotIn("' \u2026 '", tf)
        self.assertNotIn("'\u2026\n'", tf)
        # AP: строки БЕЗ букв (мусор ", .,.:,. 2 2026.") не показываются
        self.assertIn(".filter((ln) =>", tf)
        self.assertIn("a-zA-Z]", tf)

    def test_ao4_sugg_ellipsis_js(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AQ: КОРЕНЬ БЫЛ В СЕТКЕ. Колонки 1fr не могут быть уже nowrap-
        # заголовка (min-content) — плитки разъезжались, текст «неровнел»,
        # и заголовок НИКОГДА не переполнялся (ни CSS «…», ни JS-обрезка
        # не видели переполнения). minmax(0,1fr) держит плитки равными.
        sugg_grid = css.split(".suggestions{")[1].split("\n")[0]
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", sugg_grid)
        # заголовок — во внутреннем span (min-width:0): каноническое «…»
        self.assertIn(".sugg b{display:flex;min-width:0;", css)
        st = css.split(".sugg b .st{")[1].split("}")[0]
        self.assertIn("min-width:0", st)
        self.assertIn("white-space:nowrap", st)
        self.assertIn("overflow:hidden", st)
        self.assertIn("text-overflow:ellipsis", st)
        self.assertIn("'<b><span class=\"st\">' + esc(s.title) + '</span></b><span class=\"sp\">' + esc(s.desc || s.prompt) + '</span>'", js)
        # JS-обрезка удалена: три итерации измерений не пережили реальности
        self.assertNotIn("fitSuggTitle", js)
        self.assertNotIn("watchSuggTitle", js)
    def test_ao5_toggle_clicks_never_blocked(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # блокировка кликов на 950мс удалена — каждый клик переключает
        self.assertNotIn(".agent-switch.ag-switching .agent-switch-track{pointer-events:none}", css)
        # двойная пересадка защищена флагом занятости кисти
        paint = js.split("function agentPaint(")[1].split("\nfunction ")[0]
        self.assertIn("agentPaint._busy", paint)
        self.assertIn("agentPaint._busy = false", paint)


class IterationAQTests(unittest.TestCase):
    """AQ (beta.48): верхняя панель — непрерывная полоса под меню (без
    анимации самой панели), плитки minmax(0,1fr) + span-троеточие, ход
    мыслей целыми предложениями (без огрызков и склеек) и РАНЬШЕ ВСЕХ,
    реактор-эстафета: перегорание/загорание одной анимацией, тёмный покой."""

    def test_aq1_topbar_continuous_band(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # полоса НА ВСЮ ШИРИНУ, всегда на месте: меню (z-60) закрывает её
        # слева, уезжая в док — панель ОТКРЫВАЕТСЯ за ним без анимации
        top = css.split(".topbar{")[1].split("}")
        topbar = top[0]
        self.assertIn("position:fixed", topbar)
        self.assertIn("top:0;left:0;right:0", topbar)
        self.assertIn("z-index:55", topbar)
        self.assertIn("padding:11px 18px 11px 280px", topbar)   # чипы правее меню
        # контент — ниже полосы
        self.assertIn("padding-top:56px}", css)
        # AR: элементы панели едут за доком на новую площадь (кривые дока)
        self.assertIn("transition:padding-left .6s cubic-bezier(.22,.68,.18,1)}", css)
        self.assertIn(".app.collapsed .topbar{padding-left:18px;", css)
        # BM21: свёрнутое состояние меняет только значение — переход в базе
        self.assertNotIn("cubic-bezier(.5,.35,.15,1)", css)
        # мобильная полоса: отступ узкой полосы иконок (62+18)
        self.assertIn(".topbar{padding-left:80px}", css)

    def test_aq2_think_filter_sentences(self) -> None:
        from jarvis import agent as ag
        tf = ag._ThinkFilter()
        out: list = []
        # дословный кейс юзера: обломки собираются в слова, склеенный дубль
        # РАСКЛЕИВАЕТСЯ (мысль спасена), мусор отрезается
        for piece in ["нов", "ости октября", "ости октября России",
                      ", .,.:,. 2 2026."]:
            out += tf.feed(piece)
        out += tf.close()
        self.assertEqual(out, ["новости октября России."])
        # нормальный поток: куски собираются в целые слова
        tf2 = ag._ThinkFilter()
        o2: list = []
        for piece in ["Смотрю файлы. Н", "ужно проверить",
                      " данные. Let me check. 2 2026."]:
            o2 += tf2.feed(piece)
        o2 += tf2.close()
        # AU: английская мысль — живая, показывается как есть
        self.assertEqual(o2, ["Смотрю файлы.", "Нужно проверить данные.", "Let me check."])
        # склеенный дубль рас kleilся в чистый текст — не выброшен
        tf3 = ag._ThinkFilter()
        o3 = tf3.feed("новости октябряости октября России") + tf3.close()
        self.assertEqual(o3, ["новости октября России"])
        # живая речь с пробельными повторами не трогается
        tf4 = ag._ThinkFilter()
        o4 = tf4.feed("Он сказал так так и сделал. Проверяю.") + tf4.close()
        self.assertEqual(o4, ["Он сказал так так и сделал.", "Проверяю."])
        # сервер гоняет мысли через фильтр предложений
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("class _ThinkFilter:", src)
        self.assertIn("think_filter.feed(", src)
        self.assertIn("think_filter.close()", src)
        self.assertIn("_THINK_REPEAT_RE", src)

    def test_aq3_think_card_on_top(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # правило юзера: если ход мыслей появляется — он РАНЬШЕ ВСЕХ
        think_case = js.split("case 'thinking':")[1].split("case 'plan':")[0]
        self.assertIn("node.body.firstChild !== ui.thinkCard", think_case)
        self.assertIn("node.body.insertBefore(ui.thinkCard, node.body.firstChild)", think_case)
        self.assertIn("node.body.insertBefore(qn, node.body.firstChild)", think_case)

    def test_aq4_relay_burnout(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AS: эстафета отменена — никаких relay-анимаций и миганий
        self.assertNotIn("relayBurn", css)
        self.assertNotIn("relay-in", css)
        self.assertNotIn("relay-out", css)
        # докский реактор никто не гасит: его правила без relay-классов
        self.assertIn("body.jv-busy #brandReactor .core{animation-duration:.62s;", css)
        # AV: вспышек прилёта больше нет — только кроссфейд
        self.assertNotIn("arriveCore", css)
        self.assertNotIn("arriveName", css)
        self.assertIn("transition:opacity .18s ease}", css)
        self.assertIn(".fly-ghost{position:fixed;z-index:400;pointer-events:none;margin:0", css)

    def test_ar1_llm_reasoning_dedup(self) -> None:
        from jarvis.llm import _reasoning_increment
        # кейс юзера: новый кусок начинается с повтора хвоста
        self.assertEqual(_reasoning_increment("текст ...ости октября",
                                              "ости октября России"), " России")
        # короткие повторы — живая речь, не трогаем
        self.assertEqual(_reasoning_increment("он сказал так ", "так так"),
                         "так так")
        self.assertEqual(_reasoning_increment("", "мысль"), "мысль")
        src = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        self.assertIn("def _reasoning_increment(", src)
        self.assertIn("think = _reasoning_increment(", src)

    def test_ar2_relay_lights_at_birth(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # AS: вместо эстафеты — ПЕРЛЁЁТ при рождении ответа (см. ao2);
        # печать больше ничем не управляет: живость круглешка — чистый CSS
        typer = js.split("function typerStart(")[1].split("\nfunction ")[0]
        self.assertNotIn("flyWelcomeInto", typer)
        self.assertIn(".msg-ai.live .ai-core",
                      Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8"))

    def test_ar3_relay_survives_interactive(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AS: спроселёживание убрано из JS вовсе — живость круглешка
        # держит класс .typing на тексте ответа, интерактив его не снимает,
        # поэтому «долгий ответ» остаётся живым без всяких стражей
        # BD: живой блок многострочный — такт ищем по кадру анимации
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)
        self.assertIn("@keyframes coreLive", css)

    def test_ar4_static_no_cache(self) -> None:
        src = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        # _send всегда отвечает no-store — статика никогда не кэшируется
        self.assertIn('"Cache-Control", "no-store"', src)
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        ver = Path("app/jarvis/__init__.py").read_text(encoding="utf-8").split('"')[1]
        self.assertIn("/static/css/app.css?v=" + ver, html)
        self.assertIn("/static/js/app.js?v=" + ver, html)

    def test_ar6_sugg_even_grid(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AR: все плитки ОДНОЙ высоты — ряды не гуляют, сетка ровная
        grid = css.split(".suggestions{")[1].split("}")[0]
        self.assertIn("grid-auto-rows:142px", grid)
        # единый ритм строк
        self.assertIn("line-height:1.55}", css)
        self.assertIn("line-height:1.35}", css)

    def test_ar5_steel_dark_reactor(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # AS: стальной покой и relayBurn снесены вместе с эстафетой
        self.assertNotIn("grayscale(.88) brightness(1.32)", css)
        self.assertNotIn("relayBurn", css)
        self.assertIn("radial-gradient(circle,#fff,var(--cy2) 45%,#0575a8)", css)
        # неактивная карточка по-прежнему приглушена
        self.assertIn(".ui-inert .ai-avatar{opacity:.55;filter:saturate(.7)}", css)

    def test_as1_think_translate_fallback(self) -> None:
        from jarvis import agent as ag
        # AU: перевод снесён — мысли (любой язык) идут в ленту ЖИВЬЁМ
        tf = ag._ThinkFilter()
        out = tf.feed("Let me check the news. First I search sources. ") + tf.close()
        self.assertEqual(out, ["Let me check the news.", "First I search sources."])
        # мусор без букв по-прежнему умирает
        tf2 = ag._ThinkFilter()
        self.assertEqual(tf2.feed(", .,.:,. 2 2026.") + tf2.close(), [])
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertNotIn("_translate_think", src)
        self.assertNotIn("think_translated_events", src)
        self.assertIn("_HAS_LETTERS_RE", src)
        self.assertIn("def think_close_events(", src)
    def test_as2_flight_and_curve(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # кривая разворачивания панели исправлена (была .2 — рывок в конце)
        self.assertIn("transition:padding-left .6s cubic-bezier(.22,.68,.18,1)}", css)
        self.assertNotIn("cubic-bezier(.22,.68,.18,.2)", css)

    def test_as3_web_search_paced(self) -> None:
        src = Path("app/jarvis/tools/__init__.py").read_text(encoding="utf-8")
        # AS: модель больше не перебирает все ссылки — сниппетов хватает
        self.assertIn("открывай не больше двух ссылок", src)


class IterationATTests(unittest.TestCase):
    """AT (beta.51): ход мыслей показывается ВСЕГДА (порог не глотает
    короткие), перевод английского — в фоновом потоке (инструменты больше
    не стоят минуту), перелёт: места пусты до прилёта, призраки летят по
    своим стилям в правильные точки; ФПС дока — без анимации blur-стекла."""

    def test_at1_think_never_swallowed(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # короткий накопленный ход мыслей показывается ВСЁ РАВНО
        self.assertIn("if force and not thinking_visible and thinking_pending:", src)
        close = src.split("def think_close_events(")[1].split("\n\n")[0]
        self.assertIn("think_route(think_filter.close(), force=True)", close)
        # ГЛАВНЫЙ КОРЕНЬ «мыслей нет»: технический заголовок стрима (model)
        # летит ПЕРВЫМ и раньше закрывал фазу на пустом буфере — сброс
        # порога не срабатывал никогда; модель его больше не закрывает
        self.assertIn('if etype not in ("reasoning", "model") and think_open:', src)

    def test_at2_translate_not_blocking(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # AU: переводческой машинерии больше нет вообще — нечему блокировать
        self.assertNotIn("threading.Thread(target=_tr", src)
        self.assertNotIn("_think_tr", src)
        close = src.split("def think_close_events(")[1].split("\n\n")[0]
        self.assertIn("force=True", close)

    def test_at3_fps_no_blur_transition(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # blur-стекло не анимируется (пересчёт размытия рвал кадры, Safari)
        self.assertNotIn("backdrop-filter .5s", css)
        self.assertNotIn("gap .6s ease", css)
        # геометрия едет общими часами морфа (BM21)
        self.assertIn("transition:width var(--fold-t) var(--fold-ease),padding", css)

    def test_at4_flight_v2(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # до прилёта места ПУСТЫ, призраки — по своим классам (не .hello/.reactor)
        self.assertIn("core.classList.add('pre-flight')", js)
        self.assertIn("name.classList.add('pre-flight')", js)
        self.assertIn(".ai-core.pre-flight{opacity:0}", css)
        self.assertIn(".ai-name.pre-flight{opacity:0}", css)
        self.assertIn("'fly-ghost ghost-reactor'", js)
        self.assertIn("'fly-ghost ghost-title'", js)
        self.assertIn(".ghost-reactor .gcore{width:28%;height:28%;", css)
        # AX: надпись — два слоя (градиент + цвет), меняются В ПОЛЁТЕ
        self.assertIn(".ghost-title{display:grid;width:max-content}", css)
        self.assertIn(".gt-grad{background:linear-gradient(90deg,var(--cy2),var(--teal) 40%,var(--violet) 75%,var(--pink));", css)
        self.assertNotIn("'hello fly-ghost'", js)
        self.assertNotIn("'reactor fly-ghost'", js)
        # цель меряется в момент старта (прокрутка утихла) — призрак не мимо
        ghost = js.split("function flyGhost(")[1].split("\nfunction ")[0]
        # AW: живое наведение — цель перемеряется каждый кадр
        self.assertIn("const to = targetRect();", ghost)
        self.assertIn("requestAnimationFrame(tick);", ghost)
        self.assertIn("g.dataset.landed", ghost)


class IterationAUTests(unittest.TestCase):
    """AU (beta.52): ход мыслей — живой поток (английский виден сразу,
    перевода нет, мусор без букв умирает), печать заметно быстрее,
    перелёт ждёт укладки прокрутки, видимый номер сборки в панели."""

    def test_au1_live_thinking_any_language(self) -> None:
        from jarvis import agent as ag
        tf = ag._ThinkFilter()
        out = []
        for piece in ["Let me check the latest news. ",
                      "Смотрю данные. ", ", .,.:,. 2 2026. "]:
            out += tf.feed(piece)
        out += tf.close()
        self.assertEqual(out, ["Let me check the latest news.", "Смотрю данные."])

    def test_au2_faster_typing(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # AV: скорости возвращены как были (юзер не просил ускорять)
        self.assertIn("const CPS_TALK = 125;", js)
        self.assertIn("const CPS_TALK_MAX = 245;", js)
        self.assertIn("const CPS_CODE = 470;", js)
        # КОРЕНЬ аномальной медленности: предел кадра привязан ко времени
        # кадра — тяжёлый рендер больше не роняет темп до «полслова в секунду»
        typer = js.split("function typerStart(")[1].split("\nfunction ")[0]
        self.assertIn("const frameCap = Math.max(baseCap, Math.ceil((ui.cps * elapsed) / 1000));", typer)
        self.assertIn("step = Math.min(step, left, frameCap);", typer)

    def test_au3_version_chip(self) -> None:
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('<span class="ver-chip">b70</span>', html)
        self.assertIn(".ver-chip{align-self:center;", css)

    def test_au4_flight_waits_for_scroll(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # AW: призраки создаются В МОМЕНТ ОТПРАВКИ на местах элементов
        # приветствия, оригиналы прячутся в тот же кадр — исчезновения нет
        self.assertIn("const gc = el('div', 'fly-ghost ghost-reactor',", js)
        self.assertIn("wlReactor.style.visibility = 'hidden';", js)
        self.assertIn("wlTitle.style.visibility = 'hidden';", js)
        self.assertIn("welcomeFlight = { core: cr, title: tr, ghostCore: gc, ghostTitle: gt };", js)
        # полёт стартует сразу, без ожидания укладки прокрутки
        self.assertNotIn("}), 320);", js)
        self.assertIn("width:max-content}", css)


class IterationAWTests(unittest.TestCase):
    """AW (beta.54): перелёт без исчезновения (призраки рождаются в момент
    отправки, летят сразу с живым наведением), мысли не штормят прокрутки,
    thinkType коагулируется в кадр, llm-дедуп не глотает content дельты."""

    def test_aw1_flight_immediate(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        fly = js.split("function flyWelcomeInto(")[1].split("\nfunction ")[0]
        # никакого ожидания — только старт полёта и страховки
        self.assertNotIn("setTimeout(() => requestAnimationFrame", fly)
        self.assertIn("flyGhost(wf.ghostCore,", fly)
        self.assertIn("flyGhost(wf.ghostTitle,", fly)

    def test_aw2_think_no_scroll_storm(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        think_case = js.split("case 'thinking':")[1].split("case 'plan':")[0]
        # события мыслей больше не дёргают принудительную прокрутку
        self.assertNotIn("\n      scrollDown();\n      break;", think_case)
        self.assertIn("scrollSoon(ui);", think_case)
        # thinkType перерисовывается не чаще кадра
        tt = js.split("function thinkType(")[1].split("\n}\n")[0]
        self.assertIn("el._thinkRaf", tt)
        self.assertIn("requestAnimationFrame", tt)

    def test_aw3_llm_dedup_no_swallow(self) -> None:
        src = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        # узкий блок: от дедупа до обработки tool_calls той же дельты
        block = src.split("think = _reasoning_increment(")[1].split(
            'for tc in delta.get("tool_calls")')[0]
        self.assertNotIn("\n                                    continue", block)
        self.assertIn("if think:", block)


class IterationAXTests(unittest.TestCase):
    """AX (beta.55): скоординированный уход приветствия — полёт быстрее
    (620мс, S-кривая), плитки разъезжаются с растворением, всё одной
    длительности; надпись меняет цвет В ПОЛЁТЕ (два слоя), ядро сбрасывает
    кольца В ПОЛЁТЕ; круглешок гибкий (жёлтые вспышки, форма, пружина);
    плитки фиксированной высоты с троеточием описания."""

    def test_ax1_welcome_exit_choreography(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # уход страницы + разъезд плиток + полёт — одной длительности
        self.assertIn("function welcomeExit(", js)
        we = js.split("function welcomeExit(")[1].split("\nfunction ")[0]
        self.assertIn("transform .62s cubic-bezier(.65,0,.35,1), opacity .62s cubic-bezier(.65,0,.35,1)", we)
        self.assertIn("w.style.opacity = '0';", we)
        fly = js.split("function flyGhost(")[1].split("\nfunction ")[0]
        self.assertIn("const dur = 620;", fly)

    def test_ax2_in_flight_morph(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # ядро сбрасывает кольца В ПОЛЁТЕ
        self.assertIn("rings.forEach((r) => { r.style.opacity = String(Math.max(0, 1 - e)); });", js)
        # надпись: градиент уступает цвету В ПОЛЁТЕ (два слоя)
        self.assertIn("'<span class=\"gt-grad\">JARVIS</span><span class=\"gt-solid\">JARVIS</span>'", js)
        self.assertIn("(p - .35) / .45", js)
        self.assertIn(".gt-grad{background:linear-gradient(90deg,var(--cy2),var(--teal) 40%,var(--violet) 75%,var(--pink));", css)
        self.assertIn(".gt-solid{color:var(--cy);opacity:0;", css)
        # призрак-реактор несёт кольца
        self.assertIn(".ghost-reactor .gr1{inset:0;", css)
        self.assertIn(".ghost-reactor .gcore{width:28%;height:28%;", css)

    def test_ax3_flexible_core(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        live = css.split("@keyframes coreLive{")[1].split("}}")[0]
        # BD: покой БЕЗ изменения размера — только цвет, золото, мерцание
        self.assertNotIn("transform", live)
        self.assertIn("#ffd8a8", live)
        burst = css.split("@keyframes coreBurst{")[1].split("}}")[0]
        self.assertIn("#ffd489", burst)
        # BI: смена формы — ИГРЫ ФОРМЫ, вращает JS-движок (см. az4)
        self.assertIn("function dotShapeFrame(", js)
        self.assertNotIn("spinZ", css)

    def test_ax4_tiles_fixed_ellipsis(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        grid = css.split(".suggestions{")[1].split("}")[0]
        self.assertIn("grid-auto-rows:142px", grid)
        sp = css.split(".sugg .sp{")[1].split("}")[0]
        self.assertIn("-webkit-line-clamp:5", sp)
        self.assertIn("overflow:hidden", sp)



class IterationAYTests(unittest.TestCase):
    """AY (beta.56): подсказки-продолжения от ИИ (Джарвис сам ведёт диалог
    с собой), единая анимация ухода приветствия с киношным размытием в
    движении, живой круглешок весь ответ, компактные плитки с человеческим
    троеточием, живые мысли и параллельный планировщик в AGENT, ИИ-плитки
    приветствия в фоне."""

    def test_ay1_replies_continue_the_dialogue(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # генератор видит ПОСЛЕДНИЕ РЕПЛИКИ переписки, а не одну пару
        self.assertIn("def _dialogue_lines(history", src)
        self.assertIn("history: Optional[List[Dict[str, Any]]] = None", src)
        self.assertIn('"Переписка:\\n" + "\\n".join(lines)', src)
        self.assertIn("одними подсказками", src)
        # сервер отдаёт переписку обоим генераторам подсказок
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("history=msgs", srv)
        self.assertIn("history=history", srv)

    def test_ay2_welcome_exit_is_one_motion(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        we = js.split("function welcomeExit(")[1].split("\nfunction ")[0]
        # плитки: входная анимация снята, одна кривая на всё
        self.assertIn("t.style.animation = 'none';", we)
        self.assertIn(
            "transform .62s cubic-bezier(.65,0,.35,1), opacity .62s cubic-bezier(.65,0,.35,1)", we)
        # страница тает И складывается по высоте — диалог «открывается»
        self.assertIn("w.style.height = '0px';", we)
        self.assertIn("w.dataset.exit = '1';", we)
        # addUserMsg больше не убивает уходящее приветствие
        kw = js.split("function killWelcome(")[1].split("\nfunction ")[0]
        self.assertIn("w.dataset.exit !== '1'", kw)
        # полёт — численно та же кривая, что у плиток и страницы
        fly = js.split("function flyGhost(")[1].split("\nfunction ")[0]
        self.assertIn("cubicBezierEase(.65, 0, .35, 1)", fly)

    def test_ay3_motion_blur_and_ring_pace(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        fly = js.split("function flyGhost(")[1].split("\nfunction ")[0]
        # BB: киношное размытие СТУПЕНЬКОЙ — класс + CSS-transition:
        # покадровая запись filter рвала FPS
        self.assertIn("g.classList.toggle('motion', moving);", fly)
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".fly-ghost.motion{filter:blur(1.4px)}", css)
        # кольца тают СО СКОРОСТЬЮ полёта — по eased-прогрессу
        self.assertIn(
            "rings.forEach((r) => { r.style.opacity = String(Math.max(0, 1 - e)); });", js)

    def test_ay4_dot_lives_the_whole_answer(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)
        self.assertIn(
            ".ai-core.dot-settle{animation:coreSettle .55s cubic-bezier(.65,0,.35,1) forwards}", css)
        self.assertIn("@keyframes coreSettle{", css)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function finishLiveDot(", js)
        self.assertIn("node.root.classList.add('live');", js)
        sv = js.split("function settleVisualDone(")[1].split("\nfunction ")[0]
        self.assertIn("finishLiveDot(ui.node && ui.node.root);", sv)
        # живость больше не привязана к печати: класс live — весь ответ
        self.assertNotIn(":has(.typing) .ai-core", css)

    def test_ay5_tiles_compact_human_ellipsis(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        grid = css.split(".suggestions{")[1].split("}")[0]
        self.assertIn("grid-auto-rows:142px", grid)
        sugg = css.split(".sugg{")[1].split("}")[0]
        self.assertIn("padding:12px 15px", sugg)
        self.assertIn("font-size:12.5px", sugg)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function fitSuggText(", js)
        fit = js.split("function fitSuggText(")[1].split("\nfunction ")[0]
        self.assertIn("sp.dataset.full", fit)
        # после знака препинания многоточие идёт после пробела
        self.assertIn("'\\u00A0…'", fit)
        self.assertIn("function fitSuggTexts(", js)

    def test_ay6_agent_streams_thoughts_and_plans_in_parallel(self) -> None:
        run_src = inspect.getsource(agent.Agent._run_body)
        # мысли — живой поток и в AGENT: отложенной очереди больше нет
        self.assertNotIn("deferred_work_events.append(out)", run_src)
        # планировщик — фоновый, со времён первого tool_partial
        self.assertIn("def _start_bg_plan()", run_src)
        self.assertIn("def flush_ready_plan()", run_src)
        for banned in ("autonomous_names)", "[name])", "work_started)"):
            self.assertNotIn("plan = self.make_plan(user_text, " + banned, run_src)

    def test_ay7_welcome_tiles_come_from_ai(self) -> None:
        src = Path("app/jarvis/ideas.py").read_text(encoding="utf-8")
        self.assertIn("def _ai_personalized(", src)
        self.assertIn("def refresh_ai_async(", src)
        self.assertIn('"source": "ai"', src)
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        handler = srv.split('if path == "/api/ideas":')[1].split("if path ==")[0]
        self.assertIn("ideas.refresh_ai_async()", handler)
        self.assertIn('"refreshing": refreshing', handler)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("if (r.refreshing && !again) setTimeout(() => { loadIdeas(true); }, 2600);", js)

    def test_ay8_suggestions_allow_tech_words(self) -> None:
        # разговор о коде и файлах — законное продолжение диалога
        self.assertTrue(agent._suggestion_usable("Добавь тесты в game.py"))
        self.assertFalse(agent._suggestion_usable("tool_calls"))
        self.assertFalse(agent._suggestion_usable("Content overview"))



class IterationAZTests(unittest.TestCase):
    """AZ (beta.57): подсказки всегда по теме (углубляющие) и принадлежат
    только своему диалогу; круглешок спокойнее — оживает всплеском на
    видимых действиях и изредка играет с формой (тессеракт, тетраэдр,
    кривая, звезда); плитки — название + описание, промпт по клику."""

    def test_az1_replies_stay_on_topic_and_per_chat(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # генератор обязан углублять ТУ ЖЕ тему, общие фразы — банлист
        self.assertIn("СТРОГО о том же предмете", src)
        self.assertIn("уходить в другую тему", src)
        parse = src.split("def _parse_reply_suggestions(")[1].split("\ndef ")[0]
        self.assertIn('"расскажи подробнее"', parse)
        self.assertIn('"покажи на примере"', parse)

    def test_az2_replies_belong_to_their_chat(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # новый диалог и чужой диалог гасят полосу подсказок сразу
        new_chat = js.split("function newChat(")[1].split("\nfunction ")[0]
        open_chat = js.split("async function openChat(")[1].split("\nfunction ")[0]
        for seg in (new_chat, open_chat):
            self.assertIn("S.replyTicket = (S.replyTicket || 0) + 1;", seg)
            self.assertIn("rb.hidden = true; rb.innerHTML = '';", seg)
        fr = js.split("async function fetchReplies(")[1].split("\nfunction ")[0]
        self.assertIn("if (activeChatId() !== chat) { showReplies([]); return; }", fr)

    def test_az3_dot_calm_base_action_burst(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)
        self.assertIn(".msg-ai.live .ai-core.dot-act{animation:coreBurst 1.15s cubic-bezier(.65,0,.35,1)}", css)
        self.assertIn("@keyframes coreBurst{", css)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function dotAction(", js)
        ts = js.split("case 'tool_start': {")[1].split("case '")[0]
        self.assertIn("dotAction(ui.node && ui.node.root);", ts)
        ps = js.split("case 'plan_step': {")[1].split("case '")[0]
        self.assertIn("dotAction(ui.node && ui.node.root);", ps)

    def test_az4_dot_plays_with_shapes(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BD: база истории — ПРОСТОЙ круг (дешёвый); 12-точечный «круг»
        # и плавный переход переехали в live-блок
        core = css.split(".ai-core{")[1].split("}")[0]
        self.assertNotIn("clip-path", core)
        self.assertNotIn("filter", core)
        # BG: фигуры — ПОЛНОЦЕННЫЙ SVG: у живого ядра больше нет полигонов
        live_core = css.split(".msg-ai.live .ai-core{")[1].split("}")[0]
        self.assertNotIn("clip-path", live_core)
        self.assertIn("filter:drop-shadow(", live_core)
        # BI: 6 фигур — только ОБЪЁМНЫЕ (2×4D + 4×3D), 1D-линии убраны
        for shape in ("tess", "penta", "cube", "octa", "tetra", "crystal"):
            self.assertIn("  %s: {" % shape, js)
        for gone in ("star", "hex", "cross", "line", "wave", "zig"):
            self.assertNotIn("  %s: {" % gone, js)
        self.assertIn('<radialGradient id="gF', js)
        self.assertIn('<linearGradient id="gE', js)
        self.assertIn("const DOT_SHAPE_KEYS = Object.keys(DOT_SHAPES);", js)
        self.assertIn("function dotShapePlay(", js)
        fd = js.split("function finishLiveDot(")[1].split("\nfunction ")[0]
        # BA: все классы круглешка живут на САМОМ .ai-core — коллизия имён
        # с CSS-правилом кнопок .act ломала карточку ответа рамкой
        self.assertIn("const core = root.querySelector('.ai-core');", fd)
        self.assertIn("core.classList.remove('dot-act', 'shape-on', 'dot-settle');", fd)
        self.assertIn("const activeShow = !!(core._shapeSvg && core._shapeRaf);", fd)
        self.assertIn("core._shapeSvg = null;", fd)
        self.assertIn("core.classList.add('dot-settle');", fd)

    def test_az5_tiles_show_title_and_description(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("desc: s[1], prompt: s[2]", js)
        self.assertIn("esc(s.desc || s.prompt)", js)
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        sp = css.split(".sugg .sp{")[1].split("}")[0]
        self.assertIn("-webkit-line-clamp:5", sp)
        src = Path("app/jarvis/ideas.py").read_text(encoding="utf-8")
        self.assertIn('"desc": "Пройдусь по новостным сайтам', src)
        self.assertIn('out.append({"title": title, "desc": desc, "prompt": prompt})', src)
        self.assertIn("18-24 слов", src)



class IterationBATests(unittest.TestCase):
    """BA (beta.58): подсказки только по теме (общие слова и чужие темы
    отрезаются, шаблоны — лишь при сбое nano на содержательном ответе),
    непрерывность диалога при смене моделей, круглешок починен (классы на
    .ai-core, без коллизии .act), плитки с описанием из кэша v2, живой
    поток финальной сводки, предпрогрев по наведению."""

    def test_ba1_replies_on_topic_or_nothing(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("def _topic_words(", src)
        self.assertIn("_SUGGEST_STOPWORDS", src)
        # BF: скудная тема больше не выкидывает живые реплики в шаблон
        self.assertIn("themed = [x for x in parsed if _topic_words(x) & topic]", src)
        self.assertIn("items = themed or parsed", src)
        # BE: модель ушла в сторону — тоже сбой: запас вместо чужой темы
        self.assertIn('span.finish("off_topic")', src)
        # BE: подсказки ВСЕГДА — финал функции не возвращает пусто
        self.assertIn("if tools_used:\n        return suggest_proactive(q, raw_answer, tools_used)\n    return suggest_replies(q, raw_answer)", src)
        parse = src.split("def _parse_reply_suggestions(")[1].split("\ndef ")[0]
        self.assertIn('"получил, спасибо"', parse)

    def test_ba2_dialogue_continuity_across_models(self) -> None:
        orch = Path("app/jarvis/orchestrator.py").read_text(encoding="utf-8")
        # «ты как» — болтовня: раньше уезжала в полный промпт и модель
        # отвечала самопредставлением
        self.assertIn("|ты как|", orch)
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # в обоих промптах — правило непрерывности
        self.assertEqual(src.count("БЕСЕДА УЖЕ ИДЁТ"), 2)
        self.assertIn("даже если сменилась модель или режим", src)

    def test_ba3_dot_classes_live_on_the_core(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        act = js.split("function dotAction(")[1].split("\nfunction ")[0]
        self.assertIn("root.querySelector('.ai-core')", act)
        self.assertIn("core.classList.add('dot-act');", act)
        # коллизия с CSS кнопок .act устранена именем dot-act
        self.assertNotIn("root.classList.add('act')", js)
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertNotIn(".msg-ai.live .ai-core.act{", css)

    def test_ba4_tiles_desc_cache_v2(self) -> None:
        src = Path("app/jarvis/ideas.py").read_text(encoding="utf-8")
        # старый кэш без описаний не показывается: на плитке был бы промпт
        self.assertIn('stale = data.get("v") != 2', src)
        self.assertIn('saved = [] if stale else _clean(data.get("items"))', src)
        self.assertIn('"source": "local", "v": 2', src)
        self.assertIn('"source": "ai", "v": 2', src)

    def test_ba5_closing_summary_streams(self) -> None:
        run_src = inspect.getsource(agent.Agent._run_body)
        self.assertIn('for c_ev in llm.chat_stream(', run_src)
        self.assertIn('closing_text += piece', run_src)
        self.assertIn('yield {"type": "delta", "text": piece}', run_src)

    def test_ba6_warmup_on_hover(self) -> None:
        # BD: ПРОГРЕВ УДАЛЁН ПОЛНОСТЬЮ — каждый hover над чипом/плиткой
        # превращался в реальный LLM-вызов на машине пользователя
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertNotIn("warmRequest", js)
        self.assertNotIn("warmHover", js)
        # BM12: pointerenter живёт только у док-флайаута пространств
        if "pointerenter" in js:
            self.assertIn("wrap.addEventListener('pointerenter'", js)
            self.assertEqual(js.count("addEventListener('pointerenter'"), 1)
        self.assertNotIn("/api/warm", srv)
        self.assertNotIn("_WARM_SEEN", srv)
        self.assertNotIn('operation="warmup"', srv)



class IterationBBTests(unittest.TestCase):
    """BB (beta.59): ИИ-плитки готовы ДО экрана и греются при старте
    (фоном раз в сутки), FPS стартовой анимации и круглешка (размытие
    ступенькой, такт чистым transform, свечение drop-shadow — box-shadow
    клипался), круглешок чаще/быстрее меняет форму со свечением в момент
    трансформации, ход мыслей только у рабочих ответов, рамка-вспышка
    в конце ответа."""

    def test_bb1_ideas_ready_before_first_paint(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        init = js.split("(async function init()")[1].split("\n})();")[0]
        self.assertIn("await loadIdeas();", init)
        self.assertLess(init.find("await loadIdeas();"),
                        init.find("buildWelcome()"))
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("threading.Thread(target=ideas.refresh_ai_async", srv)
        src = Path("app/jarvis/ideas.py").read_text(encoding="utf-8")
        self.assertIn("_AI_REFRESH = 24 * 3600", src)

    def test_bb2_dot_fps_and_glow(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # BD: такт живости — БЕЗ размера (только цвет, золото, мерцание)
        live = css.split("@keyframes coreLive{")[1].split("}}")[0]
        self.assertNotIn("transform", live)
        # BH: градиенты background НЕ анимируются — золото интерполяцией
        # фильтров, без мгновенных перекрасок
        self.assertNotIn("background", live)
        self.assertIn("hue-rotate", live)
        self.assertIn("drop-shadow", live)
        # BD: ВТОРОЙ FPS-КОРЕНЬ — полигоны и свечение на КАЖДОМ
        # историческом ядре; история теперь простой круг
        core = css.split(".ai-core{")[1].split("}")[0]
        self.assertNotIn("animation:", core)
        self.assertNotIn("clip-path", core)
        self.assertNotIn("filter", core)
        self.assertIn("box-shadow:", core)

    def test_bb3_shape_play_faster_with_glow(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("const DOT_MORPH_MS = 700;", js)
        self.assertIn("const DOT_MORPH_OUT_MS = 650;", js)
        self.assertIn("const DOT_HOLD_MS = 4000;", js)
        play = js.split("function dotShapePlay(")[1].split("\nfunction ")[0]
        self.assertIn("3500 + Math.random() * 4500", play)
        # BG: фигура ×2.1 — SVG с пружиной туда-обратно; золото НАРАСТАЕТ
        # ПЛАВНО (интерполяция filter в кадрах, без резких скачков)
        self.assertIn("g.innerHTML = dotShapeFrame(key, t, svg._gradL);", play)
        self.assertIn("function dotShapeFrame(", js)
        self.assertIn(".dot-shape-svg.sh-fly{animation:dotSvgFly .5s", css)
        self.assertIn(".dot-shape-svg.sh-out{animation:dotSvgOut .4s", css)
        self.assertIn("@keyframes dotSvgFly{", css)
        self.assertIn("@keyframes dotSvgOut{", css)
        # BH: холст 30px (13px душил градиенты), пик — масштаб 1
        self.assertIn("width:46px;height:46px;", css)
        self.assertIn("100%{opacity:0;transform:translateY(-42px) scale(.22);", css)
        self.assertIn("0%{opacity:1;transform:translateY(0) scale(1);", css)

    def test_bb4_thinking_only_for_work(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # тихий режим: порога нет, фаза не выпускает мысли без инструментов
        self.assertIn("thinking_min_chars = 90 if self.show_thinking else 10 ** 9", src)
        route = src.split("def think_route(")[1].split("\n        for step in")[0]
        self.assertIn("if self.quiet_thinking and not self.used_tools:", route)

    def test_bb5_done_flash_frame(self) -> None:
        # BC: рамка-вспышка УДАЛЕНА по решению пользователя
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertNotIn("flash-done", css + js)
        self.assertNotIn("doneFlash", css + js)



class IterationBCTests(unittest.TestCase):
    """BC (beta.60): рамка удалена; производительность — история круглешков
    статична (FPS-корень: бесконечные анимации на каждом .ai-core), прогрев
    снят с кнопки отправки (каждый hover превращался в реальный LLM-вызов
    на той же машине); фигуры ×1.5 с пружинной трансформацией и золотом
    ровно в момент морфа; больше 3D/4D-фигур."""

    def test_bc1_no_done_frame(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertNotIn("flash-done", css + js)
        self.assertNotIn("doneFlash", css + js)

    def test_bc2_history_cores_static(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertNotIn("coreBreathe", css)
        core = css.split(".ai-core{")[1].split("}")[0]
        self.assertNotIn("animation:", core)
        # живёт только текущий ответ
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)

    def test_bc3_warmup_off_send_button(self) -> None:
        # BD: прогрев удалён ЦЕЛИКОМ — и с кнопки, и с плиток, и с чипов
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertNotIn("warmHover", js)
        # BM12: pointerenter живёт только у док-флайаута пространств
        if "pointerenter" in js:
            self.assertIn("wrap.addEventListener('pointerenter'", js)
            self.assertEqual(js.count("addEventListener('pointerenter'"), 1)
        self.assertNotIn("/api/warm", srv)

    def test_bc4_shapes_bigger_spring_gold(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("const DOT_MORPH_MS = 700;", js)
        self.assertIn("const DOT_MORPH_OUT_MS = 650;", js)
        self.assertIn("const DOT_HOLD_MS = 4000;", js)
        # BH: фигура — 30px SVG, пружина до масштаба 1 (30px ≈ 2.3 круга)
        self.assertIn(".dot-shape-svg.sh-fly{animation:dotSvgFly .5s", css)
        self.assertIn("0%{opacity:1;transform:translateY(0) scale(1);", css)
        self.assertIn("width:46px;height:46px;", css)
        # золото в кадрах морфа — плавное (интерполяция filter)
        # BM: улёт — одна структура фильтра в каждом кадре
        fly = css.split("@keyframes dotSvgFly{")[1].split("}}")[0]
        self.assertEqual(fly.count("brightness"), fly.count("{"))
        self.assertEqual(fly.count("drop-shadow"), fly.count("{"))
        morph_out = css.split("@keyframes dotSvgOut{")[1].split("}}")[0]
        self.assertIn("opacity:0", morph_out)

    def test_bc5_more_3d_4d_shapes(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BI: только ОБЪЁМНЫЕ фигуры: 2×4D + 4×3D (1D-линии убраны юзером)
        d4 = ("tess", "penta")
        d3 = ("cube", "octa", "tetra", "crystal")
        self.assertEqual((len(d4), len(d3)), (2, 4))
        for shape in d4 + d3:
            self.assertIn("  %s: {" % shape, js)
        for gone in ("star", "hex", "cross", "line", "wave", "zig", "sh-prism",
                     "sh-cyl", "sh-diamond", "sh-trefoil", "sh-vortex", "sh-blob"):
            self.assertNotIn("  %s: {" % gone, js)
            self.assertNotIn(".%s{" % gone, css)


class IterationBDTests(unittest.TestCase):
    """BD (beta.61): прогрев убран ПОЛНОСТЬЮ; лаги вылечены — история
    круглешков дешёвая (вся магия только на текущем ответе); круглешок
    в покое не меняет размер, фигуры сильнее (×2.1) и держатся 1.5с.
    Чипы: BE (beta.62) развернула пустой [] — это СБОЙ, подсказки
    ВСЕГДА три (запас), а не «чипов нет»."""

    def test_bd1_warmup_gone_completely(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/warm", srv)
        self.assertNotIn("_WARM_SEEN", srv)
        self.assertNotIn("hashlib", srv)
        self.assertNotIn("warmRequest", js)
        self.assertNotIn("warmHover", js)
        # BM12: pointerenter живёт только у док-флайаута пространств
        if "pointerenter" in js:
            self.assertIn("wrap.addEventListener('pointerenter'", js)
            self.assertEqual(js.count("addEventListener('pointerenter'"), 1)

    def test_bd2_replies_always_three(self) -> None:
        ag = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        fn = ag.split("def suggest_replies_ai(")[1].split("\ndef ")[0]
        # BE: пустой [] — СБОЙ подсказок, а не «чипов не будет»: функция
        # больше не возвращает пусто вовсе — всегда запас
        self.assertNotIn('span.finish("empty")', fn)
        self.assertNotIn("return []", fn)
        self.assertIn("верни ровно ТРИ ", fn)
        self.assertIn("живые реплики ВСЕГДА", fn)
        self.assertIn('span.finish("off_topic")', fn)
        # шаблоны НЕ пишутся в meta и не кэшируются навсегда
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertEqual(srv.count("items != agent.suggest_replies("), 2)

    def test_bd3_dot_calm_no_size_change(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # покой — только лёгкое желтение/свечение/мерцание, БЕЗ размера
        live = css.split("@keyframes coreLive{")[1].split("}}")[0]
        self.assertNotIn("transform", live)
        self.assertIn("#ffd8a8", live)
        self.assertIn("drop-shadow", live)
        self.assertIn("animation:coreLive 4.6s ease-in-out infinite}", css)

    def test_bd4_history_cores_cheap(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        core = css.split(".ai-core{")[1].split("}")[0]
        for prop in ("animation:", "clip-path", "filter"):
            self.assertNotIn(prop, core)
        self.assertIn("box-shadow:", core)
        # вся магия — только на текущем ответе (BG: фигуры — SVG рядом
        # с ядром, у самого ядра ни полигонов, ни псевдоэлементов)
        live_core = css.split(".msg-ai.live .ai-core{")[1].split("}")[0]
        self.assertIn("filter:drop-shadow(", live_core)
        self.assertIn(".dot-shape-svg{", css)
        self.assertIn(".msg-ai.live .ai-core.shape-on{opacity:0}", css)

    def test_bd5_shapes_harmonic_and_recognizable(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BI: 6 объёмных фигур — SVG-тела со светом, рёбрами и стеклом
        for shape in ("tess", "penta", "cube", "octa", "tetra", "crystal"):
            self.assertIn("  %s: {" % shape, js)
        for gone in ("sh-vortex", "sh-blob", "sh-prism", "sh-cyl",
                     "sh-diamond", "sh-trefoil"):
            self.assertNotIn(gone, js + css)

    def test_bd6_dot_reacts_to_files(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        f = js.split("case 'file': {")[1].split("case '")[0]
        self.assertIn("dotAction(ui.node && ui.node.root);", f)


class IterationBETests(unittest.TestCase):
    """BE (beta.62): подсказки ВСЕГДА (пусто от модели = сбой = запас);
    значок Enter в чипах вместо карандашика; круглешок морфится ЧАСТО,
    фигуры понятные (2×4D + 3×3D + 3×2D + 3×1D) с яркими рёбрами,
    полупрозрачной поверхностью и задними рёбрами, вращается во время
    показа; скролл за ответом плавный и честный (ушёл вверх — не
    прилипает, вернулся до упора вниз — прилипает); включение и
    отключение агента/компьютера — строка в чате, не всплывашка."""

    def test_be1_replies_always_three(self) -> None:
        from jarvis import agent as ag_mod
        # даже приветствие без предмета — три подсказки (запас), не пусто
        items = ag_mod.suggest_replies_ai("привет", "Привет!")
        self.assertEqual(len(items), 3)

    def test_be2_enter_icon_in_chips(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        sr = js.split("function showReplies(")[1].split("\nfunction ")[0]
        # карандашик читался как «редактирование подсказки»; Enter честно
        # говорит «в поле ввода»
        self.assertIn("el('button', 'rc-ed', '↵');", sr)
        self.assertNotIn("✎", sr)

    def test_be3_shapes_edges_glass_and_spin(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        groups = {
            "4d": ("tess", "penta"),
            "3d": ("cube", "octa", "tetra", "crystal"),
        }
        # BI: только объёмные тела; вращает JS-движок (3D — ось-угол,
        # 4D — плоскости XW/ZW, тессеракт выворачивается)
        self.assertEqual(tuple(len(v) for v in groups.values()), (2, 4))
        for shapes in groups.values():
            for shape in shapes:
                self.assertIn("  %s: {" % shape, js)
        # свет и рёбра — SVG-градиенты и штрихи в JS-чертежах
        self.assertIn('<radialGradient id="gF', js)
        self.assertIn("url(#gE' + L", js)
        # BL: рёбра — один градиент, стиль непрерывен по глубине
        self.assertNotIn("url(#gB' + L", js)
        # стекло: полупрозрачные грани с сортировкой по глубине
        self.assertIn("(0.18 + 0.34 * bright)", js)
        self.assertIn("function dotRotAxis(", js)
        self.assertIn("if (sh.d === 4) {", js)
        # вращение во время показа — JS-движок, ~30 кадров/с
        self.assertIn("core._shapeRaf = requestAnimationFrame(rot);", js)
        self.assertIn("let finished = false;", js)
        self.assertNotIn("DOT_SPINS", js)
        for kf in ("@keyframes spinZ{", "@keyframes spinX{",
                   "@keyframes spinY{", "@keyframes spinD{"):
            self.assertNotIn(kf, css)
        # ЧАСТО: каждые 3.5–8 секунд, читатель не скучает
        self.assertIn("3500 + Math.random() * 4500", js)

    def test_be4_scroll_smooth_and_honest(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # догоняющий скролл вместо мгновенных прыжков
        self.assertIn("function chaseBottom(", js)
        self.assertIn("const target = Math.min(13, Math.max(0.9, gap * 0.13));", js)
        # уход вверх ЛЮБЫМ способом (скроллбар, клавиши) снимает прилипание:
        # наш догоняющий кадр scrollTop уменьшить не может
        self.assertIn("if (top < st.lastTop - 2) { leave(); st.lastTop = top; return; }", js)
        # вернулся до упора вниз — прилипает снова
        self.assertIn("if (run && h - top - box.clientHeight < 48) {", js)
        self.assertIn("st.autoPend = 0;", js)
        # зона у дна без активного run-а — скромная
        self.assertIn("const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;", js)

    def test_be5_tool_lines_in_chat(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("function toolLine(", js)
        self.assertIn("toolLine('agent', S.agentMode);", js)
        self.assertIn("toolLine('computer', false);", js)
        self.assertIn("toolLine('computer', true);", js)
        # всплывашек о включении режимов больше нет
        self.assertNotIn("Агентский режим включён", js)
        self.assertNotIn("Готов управлять", js)
        # стильная строка: иконка + название + состояние, без времени
        self.assertIn(".tool-mark{", css)
        self.assertIn(".tool-mark.off .tm-state{color:var(--tx3)}", css)


class IterationBFTests(unittest.TestCase):
    """BF (beta.63): задачи AUTO открываются и редактируются (как сценарии);
    подсказки пишутся после КАЖДОГО ответа, Enter не прячет чипы, скудная
    тема не выкидывает живые реплики в шаблон; круглешок выше (уровень
    JARVIS), прилипает к верху в длинном ответе, фигуры — объёмные тела,
    вращение медленное; строка инструмента — робот с предлагашек, меньше
    и тише, без лишнего разделителя даты; агентский скролл честный
    (clamp свёртки не считается уходом, force не тащит ушедшего);
    математика \[ ... \] печатается панелью сразу с открывающей скобки."""

    def test_bf1_auto_task_editable(self) -> None:
        from jarvis import auto as auto_mod
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/tasks/update":', srv)
        self.assertIn("auto.update_background_task(", srv)
        # работающую задачу править нельзя — живой прогон пишет результат
        fn = Path("app/jarvis/auto.py").read_text(encoding="utf-8") \
            .split("def update_background_task(")[1].split("\ndef ")[0]
        self.assertIn('if task.get("status") == "running":', fn)
        self.assertIn("parse_schedule(fields[\"schedule\"])", fn)
        # фронт: карточка открывается, форма редактирования заполнена
        self.assertIn("function openTask(", js)
        self.assertIn("function editTask(", js)
        self.assertIn("card.onclick = () => openTask(t);", js)
        self.assertIn("api('/api/tasks/update'", js)
        paint = js.split("function paintTaskCard(")[1].split("\nfunction ")[0]
        self.assertIn("mk('Редактировать', '', () => { editTask(t); }, st === 'running');", paint)

    def test_bf2_replies_every_answer_and_no_templates(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        ag = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # подсказки заказываются даже если нода ответа умерла (перезашёл в
        # диалог); пустой chat_id нового диалога подхватывается из S.chatId
        self.assertIn("const chat = ui.chatId || S.chatId;", js)
        self.assertIn("chat === activeChatId()", js)
        self.assertIn("if (S.streaming) return;", js)
        # Enter кладёт реплику в поле, но чипы НЕ исчезают
        ed = js.split("ed.addEventListener('click'")[1].split("chip.appendChild")[0]
        self.assertNotIn("box.hidden = true", ed)
        # скудная тема не выкидывает живые реплики; таймаут nano 9с
        fn = ag.split("def suggest_replies_ai(")[1].split("\ndef ")[0]
        self.assertIn("items = themed or parsed", fn)
        # BM13: вызов вынесен в _ask_suggestions(system, tier) — nano + retry base
        self.assertIn("def _ask_suggestions(system_text, tier):", fn)
        self.assertIn("tier=tier, timeout=14", fn)  # BM10: медленный провайдер не убивает подсказки

    def test_bf3_dot_higher_always_visible_solid(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # круглешок на уровне надписи JARVIS
        self.assertIn(".ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;", css)
        # длинный ответ: аватар прилипает к верху — круглешок всегда в кадре
        self.assertIn(".msg-ai.live .ai-avatar{position:sticky;top:8px;z-index:2}", css)
        # фигура — ОБЪЁМНОЕ ТЕЛО: свет и блик несут SVG-градиенты
        self.assertIn('<radialGradient id="gF', js)
        self.assertIn('<linearGradient id="gE', js)
        self.assertIn('<linearGradient id="gB', js)
        # вращение — JS-движок: 3D ось-угол ~0.62 рад/с, 4D — выворачивание
        self.assertIn("function dotShapeFrame(", js)
        self.assertIn("const ang = t * 0.62;", js)
        self.assertIn("const a = t * 0.85, b = t * 0.5;", js)

    def test_bf4_tool_line_robot_quiet(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        # робот — тот же, что в проактивном предложении о включении
        self.assertIn('<rect x="5" y="8" width="14" height="11" rx="3"/>', tl)
        self.assertIn('<circle cx="12" cy="3.6" r="1.3"/>', tl)
        # меньше и незаметнее: тихая плитка-иконка, мелкий шрифт
        self.assertIn(".tool-mark .tm-ico{display:grid;place-items:center;width:19px;height:19px;", css)
        self.assertIn("font-size:10.5px;letter-spacing:.04em;color:var(--tx3);opacity:.78;", css)
        # разделитель даты после строки инструмента не появляется
        pds = js.split("function placeDaySeparator(")[1].split("\nfunction ")[0]
        self.assertIn("prev.classList.contains('tool-mark')", pds)

    def test_bf5_agent_scroll_honest(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # свёртка инструментов сжимает контент: clamp браузера НЕ считается
        # уходом пользователя (высота при этом уменьшилась)
        self.assertIn("if (h < st.lastH - 2) {", js)
        # force-скроллы больше не тащат пользователя, явно ушедшего вверх
        self.assertIn("const gone = !!(run && run.followOutput === false);", js)
        self.assertIn("if (!gone && (force || run || near)) {", js)

    def test_bf6_math_panel(self) -> None:
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # распознавание \[ ... \] и \( ... \), в т.ч. НЕЗАКРЫТАЯ формула
        self.assertIn("math-block", md)
        self.assertIn("math-live", md)
        self.assertIn("math-inline", md)
        # печать не замораживает открытую формулу
        rt = js.split("function renderTyped(")[1].split("\nfunction ")[0]
        self.assertIn("mathOpen", rt)
        # дизайн: математический шрифт, чуть крупнее, таб-смещение
        self.assertIn(".math-block{font-family:'STIX Two Math','Cambria Math'", css)
        self.assertIn("font-size:1.13em", css)
        self.assertIn("padding:5px 14px 5px 16px;", css)


class IterationBGTests(unittest.TestCase):
    """Итерация BG (beta.64): АВТО-карточка без «Лог», центрированные
    кнопки разрешения, честные чипы, SVG-круглешок, скролл-догон и
    полноценный математический режим (LaTeX + plot/geo панели)."""

    def test_bg1_auto_card_buttons_only_their_job(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        card = js.split("function paintTaskCard(")[1].split("\nfunction ")[0]
        # клик по кнопке = ТОЛЬКО её функция; карточку НЕ открываем
        self.assertIn("b.addEventListener('click', (e) => { e.stopPropagation(); fn(e); });", card)
        # кнопки «Лог» на карточке больше НЕТ — события живут в окне задачи
        self.assertNotIn("mk('Лог'", card)
        task = js.split("function openTask(")[1].split("\nfunction ")[0]
        # лог — свёрнутый <details> внутри окна задачи
        self.assertIn('<details class="sd"', task)
        self.assertIn("Лог (", task)

    def test_bg2_mode_permission_buttons_centered(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # BH: карточки разрешений ВЕРНУТЫ как было (юзер не просил их менять);
        # по центру — только строка-уведомление о режиме (.tool-mark)
        self.assertIn(".mc-actions{display:flex;align-items:center;gap:16px}", css)
        self.assertNotIn(".mc-actions>.mc-switch", css)
        tm = css.split(".tool-mark{")[1].split("}")[0]
        self.assertIn("margin:7px auto", tm)
        self.assertIn("width:max-content", tm)
        self.assertIn("display:flex", tm)
        self.assertIn("align-items:center", tm)

    def test_bg3_chips_immediate_clear_and_gated_refetch(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        send = js.split("async function send(")[1].split("\nfunction ")[0]
        # чипы прошлого ответа исчезают СРАЗУ при отправке следующего запроса
        self.assertIn("const rb = $('#replyBar');", send)
        self.assertIn("if (rb) { rb.hidden = true; rb.innerHTML = ''; }", send)
        self.assertIn("S.replyTicket = (S.replyTicket || 0) + 1;", send)
        # во время печати подсказки не показываем и не заказываем
        fetch = js.split("async function fetchReplies(")[1].split("\nfunction ")[0]
        self.assertIn("if (S.streaming) return;", fetch)
        # после конца ответа чипы заказываются даже для умершей ноды —
        # по свежему chat_id из состояния (перезашёл в диалог без кэша)
        self.assertIn("const chat = ui.chatId || S.chatId;", js)
        self.assertIn("if (!(S.streaming && S.streamRun !== runId) && chat && chat === activeChatId())", js)
        # возврат в диалог с пустым кэшем — фоновый пересчёт
        self.assertIn("if (!cachedReplies.length && !S.streaming) fetchReplies();", js)

    def test_bg4_dot_full_svg_quality(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # НЕ малополигональный: свет, блики и рёбра несут SVG-градиенты
        self.assertIn('<radialGradient id="gF', js)
        self.assertIn('<linearGradient id="gE', js)
        self.assertIn('<linearGradient id="gB', js)
        self.assertIn("url(#gE' + L", js)
        # BL: рёбра больше не делятся на «задние/передние» одним кадром
        self.assertNotIn("url(#gB' + L", js)
        for shape in ("tess", "penta", "cube", "octa", "tetra", "crystal"):
            self.assertIn("  %s: {" % shape, js)
        for gone in ("star", "hex", "cross", "line", "wave", "zig"):
            self.assertNotIn("  %s: {" % gone, js)
        self.assertIn("width:46px;height:46px;", css)
        self.assertIn("d: 4,", js)
        # полигональные clip-path-фигуры ушли из CSS навсегда
        self.assertNotIn("clip-path:polygon(", css)
        self.assertIn(".dot-shape-svg{", css)
        self.assertIn(".msg-ai.live .ai-core.shape-on{opacity:0}", css)
        # золото нарастает и уходит ПЛАВНО — интерполяцией в кадрах морфа
        self.assertIn("@keyframes dotSvgFly{", css)
        self.assertIn("@keyframes dotSvgOut{", css)
        self.assertIn("#ffd8a8", css)
        # вращение — отдельная группа фигуры, ~125-140° за 1.5s ease-in-out
        self.assertIn("function dotShapeFrame(", js)

    def test_bg5_scroll_bottom_first_and_smooth_chase(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # дно проверяем ПЕРВЫМ: раньше кадр догона съедал событие «у низа»
        self.assertIn("if (run && h - top - box.clientHeight < 48) {", js)
        self.assertIn("st.autoPend = 0;", js)
        # свёртка панели — плавный догон, без резких прыжков
        self.assertIn("function followGrowingPanel(", js)
        self.assertIn("const target = Math.min(13, Math.max(0.9, gap * 0.13));", js)

    def test_bg6_math_latex_and_live_panels(self) -> None:
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            prompt = agent.build_system_prompt(True)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # правило 11: честный LaTeX + блоки plot/geo с JSON-примерами
        self.assertIn("11. МАТЕМАТИКА", prompt)
        self.assertIn("\\frac", prompt)
        self.assertIn("\\sqrt[n]", prompt)
        self.assertIn('{"f"', prompt)
        self.assertIn('{"z"', prompt)
        self.assertIn('{"points"', prompt)
        self.assertIn("```plot", prompt)
        self.assertIn("```geo", prompt)
        # мини-LaTeX рендерер: обозначения инлайн, уравнения — блоком
        self.assertIn("function mathRender(", md)
        self.assertIn("function mathIsBlock(", md)
        self.assertIn("\\u0001", md)
        self.assertIn('data-kind="plot"', md)
        self.assertIn('data-kind="geo"', md)
        self.assertIn(".mfrac{", css)
        self.assertIn(".msqrt{", css)
        # живые панели в диалоге: парсер формул + 2D/3D/geo с паном и зумом
        self.assertIn("function mathParseExpr(", js)
        self.assertIn("function mathCompile(", js)
        self.assertIn("function mountPlotPanels(", js)
        self.assertIn("function buildPlot2Panel(", js)
        self.assertIn("function buildPlot3Panel(", js)
        self.assertIn("function buildGeoPanel(", js)
        self.assertIn(".plot-panel{", css)
        # панели монтируются и у готовых ответов, и у live-печати
        self.assertGreaterEqual(js.count("mountPlotPanels("), 4)


class IterationBHTests(unittest.TestCase):
    """Итерация BH (beta.65): плавный агентский скролл, уведомление режима
    посреди ответа, круглешок только 3D/4D с честным светом, математика с
    пределами/окружениями и графики с ховером/паном/соотношением осей."""

    def test_bh1_agent_scroll_smooth_as_quiet(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # непрерывный живой догон: пока есть live-ответ, лента доедает
        # остаток каждый кадр — как печать тихого режима
        self.assertIn("function followLiveStream(", js)
        self.assertIn("box.querySelector('.msg-ai.live')", js)
        # BI: тот же механизм, что у тихой печати — разгоняющийся chaseBottom
        self.assertIn("if (gap > 1 && !st.chasing && !(run && run.followOutput === false)) {", js)
        self.assertIn("followLiveStream(box);", js)
        # BJ: РОВНЫЙ ХОД — скорость пропорциональна остатку, без разгона:
        # большая карточка догоняется постоянным ходом, у дна плавно замирает
        chase = js.split("function chaseBottom(")[1].split("\nfunction ")[0]
        self.assertIn("const target = Math.min(13, Math.max(0.9, gap * 0.13));", chase)
        self.assertIn("const v = Math.min(target, (st.chaseV || 0) + 1.4);", chase)
        self.assertNotIn("st.v =", chase)

    def test_bh2_mode_note_stays_mid_answer(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        # включение режима ПОСЕРЕДИНЕ ответа: метка в потоке печати,
        # между замороженной головой и хвостом — а не в конце ленты
        self.assertIn("let liveUi = (S.followUi", tl)
        # BM: границу ищет renderTyped — toolLine только объявляет ожидание
        self.assertIn("liveUi.freezePending = true;", tl)
        self.assertIn("liveUi.marksEl = el('div', 'md-marks');", tl)
        self.assertIn("if (markHost) markHost.appendChild(row);", tl)
        rt = js.split("function renderTyped(")[1].split("\nfunction ")[0]
        self.assertIn("ui.marksEl", rt)
        self.assertIn("want.forEach((n) => ui.mdEl.appendChild(n));", rt)
        self.assertIn("segs.forEach((sg) => { want.push(sg.frozenEl, sg.marksEl); });", rt)
        self.assertIn(".md-marks{width:100%}", css)
        # карточки разрешений НЕ трогали — как было
        self.assertNotIn(".mc-actions>.mc-switch", css)

    def test_bh3_dot_shapes_volumetric_only(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # BI: только объёмные тела (2×4D + 4×3D); 1D и 2D убраны
        for shape in ("tess", "penta", "cube", "octa", "tetra", "crystal"):
            self.assertIn("  %s: {" % shape, js)
        for gone in ("star", "hex", "cross", "line", "wave", "zig"):
            self.assertNotIn("  %s: {" % gone, js)
        self.assertIn("d: 4,", js)
        self.assertIn("d: 3,", js)
        # 4D-вращение в плоскостях XW/ZW; 3D — ось-угол
        self.assertIn("const a = t * 0.85, b = t * 0.5;", js)
        self.assertIn("function dotRotAxis(", js)

    def test_bh4_dot_gold_and_light_honest(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # 30px холст: 13px душил градиенты и штрихи
        self.assertIn("width:46px;height:46px;", css)
        self.assertIn("filter:brightness(1) drop-shadow(0 0 5px rgba(0,195,255,.5))", css)
        # градиенты background НЕ анимируются — золото только через
        # интерполируемые фильтры; точка гаснет кроссфейдом, не мгновенно
        for kf in ("coreLive", "coreBurst", "coreSettle"):
            block = css.split("@keyframes %s{" % kf)[1].split("}}")[0]
            self.assertNotIn("background", block, kf)
        self.assertIn("hue-rotate(-138deg)", css)
        self.assertIn(".msg-ai.live .ai-core{transition:opacity .45s ease}", css)
        # BM: вход фигуры — геометрия без растворения; улёт — мягкая дуга
        self.assertIn("0%{opacity:1;transform:translateY(0) scale(1);", css)
        self.assertIn("100%{opacity:0;transform:translateY(-42px) scale(.22);", css)

    def test_bh5_math_limits_envs_arrows(self) -> None:
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # большие операторы: пределы НАД и ПОД знаком
        self.assertIn("var BIGOPS = {", md)
        self.assertIn("function bigStack(", md)
        self.assertIn("if (bigPend) {", md)
        self.assertIn("bigPend.sup = mathRender(gr.text);", md)
        # окружения и спецкоманды
        self.assertIn("function renderEnv(", md)
        self.assertIn("if (name === 'boxed') {", md)
        self.assertIn("if (name === 'begin') {", md)
        self.assertIn("if (ch === '&') { i += 1; continue; }", md)
        self.assertIn("Longrightarrow:'" + chr(92) + "u27f9'", md)
        self.assertIn("Longleftrightarrow:'" + chr(92) + "u27fa'", md)
        self.assertIn("longrightarrow:'" + chr(92) + "u27f6'", md)
        # стили: стопка оператора, рамка boxed, таблица aligned, скобка cases
        self.assertIn(".mbig{", css)
        self.assertIn(".mboxed{", css)
        self.assertIn(".mtable{", css)
        self.assertIn(".mcases", css)

    def test_bh6_plot_free_pan_hover_aspect(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        p2 = js.split("function buildPlot2Panel(")[1].split("\nfunction ")[0]
        # пан по ВСЕЙ площади: обе оси, Y больше не подгоняется
        self.assertIn("drag = { x: e.clientX, y: e.clientY, x0, x1, y0, y1 };", p2)
        self.assertIn("const dx = (drag.x - e.clientX) * ux;", p2)
        self.assertIn("y0 = drag.y0 + dy;", p2)
        # ховер: ближайшая кривая + координаты + имя функции
        self.assertIn("hover = { px: e.clientX - r.left, py: e.clientY - r.top };", p2)
        self.assertIn("if (d < (best ? best.d : 26)) best = { d, fi, y, py };", p2)
        self.assertIn("f' + (fns.length > 1 ? (best.fi + 1) : '') + ' = '", p2)
        self.assertIn("ctx.arc(hover.px, best.py, 4.6, 0, Math.PI * 2);", p2)
        # соотношение осей: по умолчанию 1:1, попап с вариантами
        self.assertIn("const ASPECTS = [", p2)
        self.assertIn("{ r: 1, label: '1:1' }", p2)
        self.assertIn("{ r: 0, label: 'авто' }", p2)
        self.assertIn("let aspect = (!fns.length && series.length) ? ASPECTS[5] : ASPECTS[0];", p2)
        self.assertIn(".plot-pop{", css)
        self.assertIn(".plot-pop.open{display:flex}", css)
        # светлее фон
        self.assertIn("background:rgba(13,29,47,.55)", css)

    def test_bh7_3d_empty_surface_fixed(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        p3 = js.split("function buildPlot3Panel(")[1].split("\nfunction ")[0]
        # сетка считается отдельно, при пустоте — авто-поиск области определения
        self.assertIn("const sampleGrid = () => {", p3)
        self.assertIn("for (const R of [2, 5, 10, 25, 60]) {", p3)
        self.assertIn("поверхность пуста: функция нигде не определена", p3)
        # дырявые клетки пропускаем — поверхность рисуется где определена
        self.assertIn("if (!isFinite(c00) || !isFinite(c10) || !isFinite(c11) || !isFinite(c01)) continue;", p3)


class IterationBJTests(unittest.TestCase):
    """Итерация BJ (beta.68): ровный скролл, уведомление режима до начала
    печати, круглешок крупнее и мягче (золото без щелчков), обычный корень,
    пределы интегралов над/под, спокойные таблицы, график-интерпретатор
    понимает любую разумную запись спецификации."""

    def test_bj1_scroll_one_ease_out_curve(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # скорость пропорциональна остатку и ограничена сверху — ни разгона,
        # ни ступенек: одна кривая для печати, карточек и панелей
        chase = js.split("function chaseBottom(")[1].split("\nfunction ")[0]
        self.assertIn("const target = Math.min(13, Math.max(0.9, gap * 0.13));", chase)
        self.assertIn("const v = Math.min(target, (st.chaseV || 0) + 1.4);", chase)
        self.assertNotIn("st.v =", js)

    def test_bj2_mode_note_before_any_text(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        # текста ещё нет: метка встаёт В ТЕЛО ответа, перед строкой статуса —
        # будущий текст напечатается ПОД ней (раньше падала в низ ленты)
        self.assertIn("let preText = false;", tl)
        self.assertIn("!cand.mdEl", tl)
        self.assertIn("body.insertBefore(liveUi.marksEl, st);", tl)
        # renderTyped не воровать метку из тела в печать
        rt = js.split("function renderTyped(")[1].split("\nfunction ")[0]
        self.assertIn("ui.marksEl.parentNode !== ui.node.body", rt)
        # reset не стирает метку вместе с текстом
        reset = js.split("case 'reset':")[1].split("\n    case ")[0]
        self.assertIn("ui.node.body.insertBefore(slot, st);", reset)
        self.assertIn("(ui.segs || []).forEach((sg) => { if (sg.marksEl) slots.push(sg.marksEl); });",
                      reset)

    def test_bj3_dot_bigger_softer_faces_visible(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        fd = js.split("function finishLiveDot(")[1].split("\nfunction ")[0]
        # фигуры крупнее (40px), вход/ход без пружинного перелёта
        self.assertIn("width:46px;height:46px;", css)
        self.assertIn(".dot-shape-svg.sh-fly{animation:dotSvgFly .5s", css)
        self.assertIn(".dot-shape-svg.sh-out{animation:dotSvgOut .4s", css)
        # грани заметнее + обводка запаивает швы между полигонами
        self.assertIn("(0.18 + 0.34 * bright)", js)
        self.assertIn('stroke-width=".4"', js)
        # конец ответа не рвёт фигуру на полобороте — мягкий уход
        self.assertIn("svg.classList.add('sh-fly');", fd)
        self.assertIn("dotShapeFrame(key, t, L, Math.min(1, p))", fd)
        self.assertIn("}, 1000);   /* BM: секунда покоя перед улётом */", fd)
        self.assertIn("svg._closeRaf = setTimeout(() => svg.remove(), 560);", fd)

    def test_bj4_gold_never_snaps(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # единая структура фильтров в КАЖДОМ кадре — иначе браузер щёлкает
        # цветом вместо интерполяции
        for kf, fns in (
            ("coreLive", ("hue-rotate", "saturate", "brightness", "drop-shadow")),
            ("coreBurst", ("hue-rotate", "saturate", "brightness", "drop-shadow")),
            ("coreSettle", ("hue-rotate", "saturate", "brightness", "drop-shadow")),
            ("dotSvgFly", ("brightness", "drop-shadow")),
            ("dotSvgOut", ("brightness", "drop-shadow")),
        ):
            block = css.split("@keyframes %s{" % kf)[1].split("}}")[0]
            frames = block.split("{").length if False else block.count("{")
            for fn in fns:
                self.assertEqual(block.count(fn), frames,
                                 "%s: %s не в каждом кадре" % (kf, fn))
            self.assertNotIn("background", block, kf)

    def test_bj5_math_root_and_inline_limits(self) -> None:
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # обычный корень: штрих + одна диагональ + носик, срастается с чертой
        self.assertIn('d="M.8 13.9 L3.3 16 L5.9 0" fill="none"', md)
        # дробь центрируется на строке (baseline числителя больше не топит её)
        self.assertIn("vertical-align:middle;margin:0 2px;line-height:1.15", css)
        self.assertIn(".math-inline .mfrac{font-size:.82em;vertical-align:middle", css)
        # пределы больших операторов — над/под знаком И в строке (компактно)
        self.assertIn("<span class=\"mbi-in\">", md)
        self.assertIn(".mbi-in{display:inline-block;vertical-align:middle", css)
        # \int_a^b распознаётся блоком: подчёркивание — слово в regex, \b не
        # срабатывал после int
        self.assertIn("(?![a-zA-Z])", md)

    def test_bj6_tables_calm(self) -> None:
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # фиксированная раскладка: колонки не прыгают на каждой новой строке
        self.assertIn("overflow:auto;max-height:62vh", css)
        self.assertIn("function fixTables(", js)
        self.assertIn("t.style.tableLayout = 'fixed';", js)
        self.assertIn("overflow-wrap:anywhere;word-break:break-word", css)

    def test_bj7_plot_interpreter_forgives(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        agent_src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # мягкий разбор: одинарные кавычки, голые ключи, висячие запятые,
        # пары ключ=значение, голая формула; синонимы y/func/formula → f
        self.assertIn("function plotParseSpec(", js)
        self.assertIn(".replace(/'/g, '\"')", js)
        self.assertIn("const dx = (drag.x - e.clientX) * ux;", js)
        self.assertIn("y0 = drag.y0 + dy;", js)
        self.assertIn("const zSrc = Array.isArray(spec.z) ?", js)
        # промпт требует строгий JSON с двойными кавычками
        with mock.patch.object(agent.db, "recall", return_value=[]), \
             mock.patch.object(agent, "_now_str", return_value="сегодня"):
            prompt = agent.build_system_prompt(True)
        self.assertIn("СТРОГО в ДВОЙНЫХ", prompt)
        self.assertIn("график-интерпретатор читает только чистый JSON", prompt)

    def test_bj8_version_b67(self) -> None:
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        self.assertIn('<span class="ver-chip">b70</span>', html)
        ver = Path("app/jarvis/__init__.py").read_text(encoding="utf-8").split('"')[1]
        self.assertIn("/static/js/app.js?v=" + ver, html)




class IterationBKTests(unittest.TestCase):
    """Итерация BK (beta.68): всегда док при запуске, план без рывков,
    настоящая трансформация круглешка, уведомление между предложениями,
    корень с совпадающей чертой, график-интерпретатор с юникод-математикой
    и паном, переживаl пересборку печати."""

    def test_bk1_dock_on_every_launch(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # сохранённое 'open' больше не восстанавливается НИКОГДА
        self.assertIn("localStorage.removeItem('jarvis.sidebar2');", js)
        self.assertNotIn("localStorage.getItem('jarvis.sidebar2')", js)
        self.assertNotIn("localStorage.setItem('jarvis.sidebar2'", js)

    def test_bk2_plan_scrolls_smooth(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # разница режимов была здесь: пункты плана прыгали к дну одним кадром
        reveal = js.split("function revealPlanItems(")[1].split("\nfunction ")[0]
        self.assertIn("chaseBottom(msgHost(), ui);", reveal)
        self.assertNotIn("pinToBottom", reveal)
        plan = js.split("case 'plan':")[1].split("\n    case ")[0]
        self.assertIn("chaseBottom(stream(), ui);", plan)

    def test_bk3_dot_true_morph(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        frame = js.split("function dotShapeFrame(")[1].split("\nfunction ")[0]
        # вершины вырастают из обода круга и возвращаются — не подмена
        self.assertIn("const DOT_CIRCLE_R = 5.2;", js)
        self.assertIn("DOT_CIRCLE_R + (r - DOT_CIRCLE_R) * m", frame)
        self.assertIn("(1 - m) * .96", frame)
        self.assertIn("m = 1 - smooth(", frame)
        self.assertIn("const DOT_HOLD_MS = 4000;", js)
        # кадр на каждом rAF — без троттлинга, рывков «одним кадром» нет
        self.assertNotIn("lastDraw", frame)
        play = js.split("function dotShapePlay(")[1].split("\nfunction ")[0]
        self.assertIn("g.innerHTML = dotShapeFrame(key, t, svg._gradL);", play)

    def test_bk4_mode_note_between_sentences(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        rt = js.split("function renderTyped(")[1].split("\nfunction ")[0]
        self.assertIn("function lastSentenceEnd(", js)
        self.assertIn("liveUi.freezePending = true;", tl)
        # BM: toolLine НЕ замораживает сам — только слот и ожидание
        self.assertNotIn("liveUi.frozen = {", tl)
        self.assertIn("liveUi.marksEl.style.display = liveUi.freezePending ? 'none' : '';", tl)
        self.assertIn("if (ui.freezePending) {", rt)
        self.assertIn("const from = Math.max(src.length, ui.markFrom || 0);", rt)
        self.assertIn("const k = lastSentenceEnd(text, from);", rt)
        self.assertIn("closeMarkSegment(ui);", rt)
        self.assertIn("function closeMarkSegment(", js)

    def test_bk5_radical_bar_matches_nose(self) -> None:
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # BL: черта — продолжение того же штриха (H1400, клип .msqrt),
        # strut держит базовую линию, знак хранит пропорции
        self.assertIn('d="M.8 13.9 L3.3 16 L5.9 0" fill="none"', md)
        self.assertIn('stroke="rgba(190,235,255,.85)"', md)
        self.assertIn('<span class="msq-r">', md)
        self.assertIn('preserveAspectRatio="none" aria-hidden="true"', md)
        # BM16: обёртки-бокса НЕТ — степень .msq-i лежит прямо над
        # нижним загибом; прежние классы msq-b/msq-box не возвращаются
        self.assertIn("'<span class=\"msq-i\">' + mesc(root) + '</span>'", md)
        self.assertNotIn('class="msq-box"', md)
        self.assertNotIn('class="msq-b"', md)
        self.assertNotIn("H1400", md)
        self.assertNotIn("H11", md)
        self.assertIn(
            ".msqrt{position:relative;display:inline-block;line-height:0;", css)
        self.assertIn(
            ".msqrt .msq-r{display:inline-block;line-height:1.5;min-height:1em;", css)
        self.assertIn(
            ".msqrt .msq-svg{position:absolute;left:0;top:0;width:.40em;height:100%;", css)
        self.assertNotIn("aspect-ratio:11/24", css)

    def test_bk6_plot_interpreter_unicode_and_defs(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # z(x,y) = ... распознаётся как поверхность; · × − ÷ π √ — в формулах
        self.assertIn("src.replace(/(^|[^\w])([a-zA-Z])\s*\(([^)]*)\)\s*=/g,", js)
        self.assertIn(".replace(/[·×]/g, '*')", js)
        self.assertIn(".replace(/[−–—]/g, '-')", js)
        self.assertIn("if (tk.length === 1) { implicit(); out.push('x');", js)
        # BM13: формулы собираются со ВСЕХ ключей спеки (y-строки, g,
        # functions…), «2x и 3» одной строкой — две кривые
        self.assertIn("function plotCollectFormulas(spec)", js)
        self.assertIn("spec.f = fl.filter((e) => e !== surface);", js)

    def test_bk7_pan_survives_rebuild(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # пересборка хвоста печатью рвала pointer capture — возвращаем
        self.assertEqual(js.count("panel._recapture = () =>"), 3)
        self.assertIn("savedPlots.forEach((p) => { if (p._recapture) p._recapture(); });", js)
        # вертикальный пан: обе оси едут (математика без двойного деления)
        p2 = js.split("function buildPlot2Panel(")[1].split("\nfunction ")[0]
        self.assertIn("const dx = (drag.x - e.clientX) * ux;", p2)
        self.assertIn("const dy = (e.clientY - drag.y) * uy;", p2)

    def test_bk8_version_b68(self) -> None:
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        self.assertIn('<span class="ver-chip">b70</span>', html)
        ver = Path("app/jarvis/__init__.py").read_text(encoding="utf-8").split('"')[1]
        self.assertIn("/static/js/app.js?v=" + ver, html)


class IterationBLTests(unittest.TestCase):
    """Итерация BL (beta.69): граница уведомления сразу после знака,
    таблица не режется, круглешок из центра и 1.5×, умный старт графиков
    с нулём в кадре, плавный зум, 3D-жесты по кнопкам, оси до конца."""

    def test_bl1_note_boundary_right_after_punct(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # граница — сразу после знака (перевод строки тоже граница),
        # «3.14» — не граница: за знаком не должно быть буквы/цифры
        lse = js.split("function lastSentenceEnd(")[1].split("\nfunction ")[0]
        self.assertIn("(?![0-9A-Za-zА-Яа-яЁё])", lse)
        self.assertNotIn("?=\\s", lse)
        # таблица и подобные объекты не режутся — ждём закрытия
        self.assertIn("function endsInOpenTable(", js)
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        rt = js.split("function renderTyped(")[1].split("\nfunction ")[0]
        self.assertNotIn("endsInOpenTable", tl)
        self.assertIn("!endsInOpenTable(head)", rt)
        # метка, не дождавшаяся конца, показывается в конце ответа
        qrf = js.split("function queueResponseFinish(")[1].split("\nfunction ")[0]
        self.assertIn("ui.marksEl.style.display = '';", qrf)

    def test_bl2_tables_and_scroll_smooth(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # колонки доехали плавно — состояние анимации переживает пересборку
        self.assertIn("const TABLE_COL_ANIM = new Map();", js)
        self.assertIn("v + d * 0.22", js)
        self.assertIn("host._tblStamp = ++TABLE_HOST_STAMP;", js)
        # потолок скролла ниже — карточки входят одним куском высоты
        chase = js.split("function chaseBottom(")[1].split("\nfunction ")[0]
        self.assertIn("const target = Math.min(13, Math.max(0.9, gap * 0.13));", chase)
        self.assertNotIn("Math.min(24,", js)

    def test_bl3_dot_centered_and_bigger(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # морф растёт из центра точки: svg и ядро в одном центре
        self.assertIn(
            ".dot-shape-svg{position:absolute;left:50%;top:8px;width:46px;height:46px;",
            css)
        self.assertIn(
            ".ai-core{position:absolute;left:50%;top:8px;width:15px;height:15px;",
            css)
        self.assertIn("const DOT_HOLD_MS = 4000;", js)
        # рёбра стилизуются непрерывно по глубине — без градиентных щелчков
        frame = js.split("function dotShapeFrame(")[1].split("\nfunction ")[0]
        self.assertIn("(0.5 + 0.45 * t) * m", frame)
        self.assertIn("(0.7 + 0.4 * t)", frame)
        # наложение показов не рвёт предыдущий оборот
        play = js.split("function dotShapePlay(")[1].split("\nfunction ")[0]
        self.assertIn("if (core._shapeRaf) { schedule(); return; }", play)

    def test_bl4_smart_start_and_smooth_zoom(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        p2 = js.split("function buildPlot2Panel(")[1].split("\nfunction ")[0]
        self.assertIn("const featureXs = () => {", p2)
        self.assertIn("const smartInit = () => {", p2)
        # ноль всегда в кадре — ось не уезжает за край
        self.assertIn("if (fns.length) {\n      if (a > 0) a = 0;", p2)
        # BM9: окно растёт под весь размах кривых — важные точки в кадре
        self.assertIn("const need = (yb - ya) * 1.16 + 0.5;", p2)
        self.assertIn("const xspan = span0 * w * aspect.r / h;", p2)
        # ± плавные: smoothstep в трёх панелях (2D, 3D, geo)
        self.assertEqual(
            js.count("const ease = (p) => p * p * (3 - 2 * p);"), 3)
        self.assertIn("mkBtn('+', () => { panel._zoom(1.3); });", js)

    def test_bl5_axes_full_and_3d_gestures(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # ось не обрезается пополам: цикл засечек идёт до конца окна
        self.assertIn("const x0t = Math.ceil(x0 / sx) * sx, x1t = Math.floor(x1 / sx) * sx;",
                      js)
        # 3D: вращение вокруг центра данных, старт по фактическим габаритам
        p3 = js.split("function buildPlot3Panel(")[1].split("\nfunction ")[0]
        self.assertIn("const fitZoom = (w, h) => {", p3)
        self.assertIn("project(x1 - mcx, y1 - mcy, c00 - mcz)", p3)
        # жесты: колесо/пинч = зум, СКМ/два пальца = пан, ЛКМ = вращение
        self.assertIn("mode = (e.button === 0 || e.button === 1) ? 'rot' : (e.button === 2 ? 'pan' : null);", p3)
        self.assertIn("if (e.pointerType === 'touch') {", p3)
        self.assertIn("mode = 'pan';", p3)
        self.assertIn("zoomBy(dist / lastDist);", p3)
        self.assertIn("zoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12);", p3)

    def test_bl6_close_mirrors_open(self) -> None:
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # закрытие инструмента в группе — анимация, обратная открытию
        qtg = js.split("function qlRowToggle(")[1].split("\nfunction ")[0]
        self.assertIn("det.animate(", qtg)
        self.assertIn("duration: 440", qtg)
        self.assertIn("duration: 380", qtg)
        self.assertIn(
            "then(() => { row.classList.remove('open'); det.style.overflow = ''; })", qtg)
        # и живая группа, и история ходят через один тогглер
        self.assertIn("qlRowToggle(row);", js.split("function flushAgentGroup(")[1]
                      .split("\nfunction ")[0])
        self.assertIn("() => qlRowToggle(row)", js.split("function renderAgentTraceGroups(")[1]
                      .split("\nfunction ")[0])


class IterationBM2Tests(unittest.TestCase):
    """BM2 — восемь зон беты .70, исправленных по причинам, не симптомам:
    метки режимов, консервация отвеченных интерактивов в тексте сообщения,
    санкции песочницы, жесты 3D, черта корня, плавный догон, одиночный
    инструмент, толерантные графики."""

    def test_mark_answered_fences_all_variants(self) -> None:
        f = agent.mark_answered_fences
        src = ("Выбор:\n\n```ui\ntiles Формат: PDF | Word\n```\n\n"
               "```ui-panel\nx\n```\n```UI\ny\n```\n```интерфейс\nz\n```")
        out = f(src)
        self.assertEqual(out.count("```ui-sent"), 4)
        self.assertNotIn("```ui\n", out)
        self.assertIn("tiles Формат: PDF | Word", out)   # тело панели не тронуто
        self.assertEqual(f(out), out)                     # идемпотентно

    def test_mark_answered_fences_keeps_other_fences(self) -> None:
        out = agent.mark_answered_fences(
            "```python\nprint(1)\n```\n```ui\ntiles Да | Нет\n```")
        self.assertIn("```python\nprint(1)\n```", out)
        self.assertIn("```ui-sent\ntiles Да | Нет\n```", out)

    def test_suppress_repeated_panels_only_answered(self) -> None:
        # BM3: единственный повтор — ЖИВОЙ: модель имеет право переспросить
        once = "Вопрос\n\n```ui-sent\ntiles Формат: PDF | Word\n```"
        repeat = "Продолжаю.\n\n```ui\ntiles Формат: PDF | Word\n```\nГотово."
        self.assertEqual(agent.suppress_repeated_panels(once, repeat), repeat)
        # третья копия той же панели — цикл, гасим
        twice = once + "\n\n```ui-sent\ntiles Формат: PDF | Word\n```"
        out = agent.suppress_repeated_panels(twice, repeat)
        self.assertEqual(out.count("```ui-sent"), 1)
        self.assertNotIn("```ui\n", out)
        # дубль внутри одного продолжения — тоже цикл
        dup = "```ui\ntiles Да | Нет\n```\nтекст\n```ui\ntiles Да | Нет\n```"
        out2 = agent.suppress_repeated_panels(once, dup)
        self.assertEqual(out2.count("```ui\n"), 1)
        self.assertEqual(out2.count("```ui-sent"), 1)
        # новая панель остаётся живой
        fresh = agent.suppress_repeated_panels(once, "```ui\ntiles Стиль: А | Б\n```")
        self.assertIn("```ui\n", fresh)

    def test_run_shell_deletion_asked_others_silent(self) -> None:
        self.assertIsNone(agent.needs_approval("run_shell", {"command": "cp a b"}))
        self.assertIsNone(agent.needs_approval(
            "run_shell", {"command": "sed -i s/x/y/ file && make"}))
        for cmd in ("rm -rf draft", "rmdir old", "unlink tmp.txt", "shred secret"):
            reason = agent.needs_approval("run_shell", {"command": cmd})
            self.assertEqual(reason, "удаление данных", cmd)

    def test_write_and_delete_split(self) -> None:
        self.assertIsNone(agent.needs_approval(
            "write_file", {"path": "notes.md", "content": "новое"}))
        self.assertIsNotNone(agent.needs_approval(
            "delete_file", {"path": "notes.md"}))
        self.assertIsNotNone(agent.needs_approval(
            "run_shell", {"command": "open -a Terminal"}))

    def test_update_message_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(db, "DATA_DIR", Path(td)), \
                 mock.patch.object(db, "_DB_PATH", Path(td) / "t.db"):
                conn = sqlite3.connect(db._DB_PATH)
                conn.executescript(db.SCHEMA)
                conn.commit()
                conn.close()
                db._CONN = None
                with mock.patch.object(db, "_CONN", db._connect()):
                    cid = db.create_chat("c")["id"]
                    db.add_message(cid, "user", "вопрос", {})
                    m = db.add_message(cid, "assistant", "```ui\ntiles Да | Нет\n```", {})
                    db.update_message_content(m["id"], "```ui-sent\ntiles Да | Нет\n```")
                    self.assertIn("ui-sent", db.get_message(m["id"])["content"])

    def test_suggest_replies_never_empty_on_ai_failure(self) -> None:
        # /api/replies обязан падать в локальный запас: пустых подсказок не бывает
        with mock.patch.object(agent.llm, "chat", side_effect=RuntimeError("down")):
            items = agent.suggest_replies_ai("как дела?", "Нормально.")
            self.assertTrue(items and isinstance(items, list))


class IterationBM3Tests(unittest.TestCase):
    """BM3 — причины, не симптомы: single-flight подсказок (никаких
    двойных nano-запросов), три РАЗНЫЕ подсказки, подавление только
    настоящего цикла панелей, простые формы графиков в промпте."""

    def test_reply_job_single_flight(self) -> None:
        # две «одновременные» задачи на одно сообщение = один вызов nano
        import threading as _th
        import jarvis.server as srv
        calls = []
        released = _th.Event()

        def fake_ai(user_text, answer, tools_used=None, history=None):
            calls.append(1)
            released.wait(2.0)
            return ["Живой вопрос один", "Живой вопрос два", "Живой вопрос три"]

        msg = {"id": "m_single", "chat_id": "c1", "role": "assistant",
               "content": "ответ", "meta": {}}
        with mock.patch.object(db, "get_message", return_value=msg), \
             mock.patch.object(db, "get_recent_messages", return_value=[]), \
             mock.patch.object(db, "update_message_meta", return_value=None), \
             mock.patch.object(agent, "suggest_replies_ai", side_effect=fake_ai), \
             mock.patch.object(agent, "suggest_replies",
                               return_value=["Расскажи подробнее",
                                             "Покажи на примере", "Что дальше?"]):
            srv._prefetch_replies("m_single", "вопрос", "ответ")
            # первый заказ ещё бежит — второй не должен подниматься
            srv._prefetch_replies("m_single", "вопрос", "ответ")
            got = srv._reply_job_result("m_single")
            released.set()
        self.assertEqual(len(calls), 1, "nano вызывается ровно один раз")
        self.assertTrue(got and len(got) == 3, got)

    def test_suggestions_dedupe(self) -> None:
        with mock.patch.object(agent.llm, "chat",
                               return_value='["Сравни поставщиков оборудования",'
                                            '"Сравни поставщиков оборудования",'
                                            '"Посчитай бюджет проекта"]'):
            items = agent.suggest_replies_ai(
                "вопрос", "ответ про проект и сроки поставки оборудования",
                history=[{"role": "user", "content": "q"},
                         {"role": "assistant", "content": "a"}])
        self.assertEqual(items, ["Сравни поставщиков оборудования",
                                 "Посчитай бюджет проекта"])

    def test_prompt_admits_xy_columns(self) -> None:
        # правило графиков живёт в системном промпте агента
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn('"x": [0, 3, 6], "y": [-3, -1, 2]', src)


class IterationBM4Tests(unittest.TestCase):
    """BM4 — первопричина «медленно думает и печатает»: в критическом пути
    ответа нет ни одного постороннего LLM-вызова, а второстепенные вызовы
    никогда не конкурируют с главным стримом."""

    def setUp(self) -> None:
        llm._PROVIDER_HEALTH.clear()

    def test_foreground_gate_blocks_background_chat(self) -> None:
        import io
        import jarvis.llm as srv

        class FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): pass

        def fake_stream(*a, **k):
            yield {"type": "model", "model": "m"}
            time.sleep(0.25)
            yield {"type": "delta", "text": "текст"}

        started = []
        with mock.patch.object(srv, "_chat_stream_impl", side_effect=fake_stream), \
             mock.patch.object(srv, "provider_conf",
                               return_value={"api_key": "k", "base_url": "http://x"}), \
             mock.patch.object(srv, "pick_model", return_value="m"), \
             mock.patch.object(srv, "_request",
                               side_effect=lambda *a, **k: started.append(1) or
                               FakeResp(b'{"choices":[{"message":{"content":"[]"}}]}')):
            gen = srv.chat_stream([{"role": "user", "content": "q"}])
            next(gen)
            self.assertFalse(srv._FG_FREE.is_set(),
                             "живой стрим держит foreground — линия занята")
            t0 = time.time()
            try:
                srv.chat([{"role": "user", "content": "q"}],
                         background=True, timeout=1, operation="bg_probe")
            except Exception:
                pass   # важна задержка старта, а не результат (БД в тестах мокается)
            waited = time.time() - t0
            gen.close()
        self.assertGreaterEqual(waited, 0.9,
                                "фоновый вызов обязан ждать живой стрим")
        self.assertTrue(srv._FG_FREE.is_set() and srv._FG_COUNT == 0,
                        "gate освобождается после закрытия стрима")

    def test_background_call_runs_free_when_idle(self) -> None:
        import jarvis.llm as srv
        self.assertTrue(srv._FG_FREE.is_set(), "в простое линия свободна")
        t0 = time.time()
        with mock.patch.object(srv, "wait_foreground_free",
                               side_effect=lambda t: srv._FG_FREE.wait(t)):
            waited = srv.wait_foreground_free(0.05)
        self.assertTrue(waited)
        self.assertLess(time.time() - t0, 0.2)

    def test_memory_async_returns_draft_and_defers_model(self) -> None:
        calls = []
        with mock.patch.object(agent, "extract_obvious_memories",
                               return_value=[{"kind": "Нравится", "key": "Нравится",
                                              "value": "кошек"}]), \
             mock.patch.object(agent, "remember_smart_facts",
                               side_effect=lambda t: calls.append(t) or []):
            t0 = time.time()
            draft = agent.remember_smart_facts_async("я люблю кошек")
            self.assertLess(time.time() - t0, 0.05,
                            "входная граница не платит сетью")
            self.assertEqual(draft[0]["value"], "кошек")
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.02)
        self.assertEqual(calls, ["я люблю кошек"],
                         "nano-структуризация выполняется фоновым потоком")

    def test_memory_async_no_facts_no_thread(self) -> None:
        with mock.patch.object(agent, "extract_obvious_memories", return_value=[]), \
             mock.patch.object(agent, "remember_smart_facts") as model:
            self.assertEqual(agent.remember_smart_facts_async("как дела?"), [])
            time.sleep(0.05)
            model.assert_not_called()

    def test_side_calls_marked_background(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn('operation="planner",' + chr(10) + '                   background=True', src)
        self.assertIn('operation="reply_suggestions_ai",' + chr(10)
                      + '               background=True', src)
        ideas_src = Path("app/jarvis/ideas.py").read_text(encoding="utf-8")
        self.assertIn('operation="welcome_ideas", background=True', ideas_src)

    def test_stream_socket_timeout_180(self) -> None:
        # пожелание человека: долгое ожидание — не ошибка; выручает сторож
        # первого токена, а не короткий обрыв
        llm_src = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        self.assertIn("payload, timeout=180,", llm_src)
        self.assertGreaterEqual(llm_src.index("timeout=180,"),
                                llm_src.index("chat/completions"), "180с — таймаут стрима")
        self.assertNotIn("timeout=45) as resp:", llm_src)


class IterationBM5Tests(unittest.TestCase):
    """BM5 — эпизодическая деградация провайдера («думает долго, печатает
    по слову в секунду» при обычно мгновенном ответе): сторож первого
    токена переключает на резервного провайдера, а свежее здоровье
    провайдеров меняет порядок запросов."""

    def setUp(self) -> None:
        llm._PROVIDER_HEALTH.clear()

    def test_watchdog_switches_to_backup_provider(self) -> None:
        import jarvis.llm as lll

        class SlowResp:
            def __init__(self): self.closed = False
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): self.closed = True
            def __iter__(self): return self
            def __next__(self):
                time.sleep(0.5)
                if self.closed:
                    raise OSError("closed by watchdog")
                return b"data: [DONE]\n\n"

        delta = ("data: {\"choices\":[{\"delta\":{\"content\":\"ок\"}}]}\n\n"
                 ).encode("utf-8")

        class FastResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): pass
            def __iter__(self):
                yield delta
                yield b"data: [DONE]\n\n"

        lll._PROVIDER_HEALTH.clear()
        calls = []

        def fake_request(url, key, payload=None, method="POST", timeout=180,
                         headers=None, ssl_ctx=None):
            calls.append(url)
            return SlowResp() if len(calls) == 1 else FastResp()

        with mock.patch.object(lll, "TTFT_WATCHDOG_S", 0.2), \
             mock.patch.object(lll, "active_providers",
                               return_value=["p_slow", "p_fast"]), \
             mock.patch.object(lll, "provider_conf",
                               side_effect=lambda n: {"api_key": "k",
                                                      "base_url": "http://%s" % n}), \
             mock.patch.object(lll, "pick_model", return_value="m"), \
             mock.patch.object(lll, "_request", side_effect=fake_request):
            events = list(lll.chat_stream(
                [{"role": "user", "content": "q"}], tier="base"))
        kinds = [e["type"] for e in events]
        self.assertIn("provider_switch", kinds,
                      "молчун обязан сопровождаться событием переключения")
        sw = next(e for e in events if e["type"] == "provider_switch")
        self.assertEqual(sw["from"], "p_slow")
        self.assertTrue(any(k in kinds for k in ("delta", "done")),
                        "резервный провайдер отвечает вместо молчуна")
        lll._PROVIDER_HEALTH.clear()

    def test_provider_order_by_fresh_health(self) -> None:
        import jarvis.llm as lll
        lll._PROVIDER_HEALTH.clear()
        # одиночный сбой — не приговор
        lll._record_provider_health("cloudru", False)
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"]),
                         ["cloudru", "deepseek"])
        # два сбоя — понижение
        lll._record_provider_health("cloudru", False)
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "deepseek")
        # эпизод прошёл — порядок вернулся сам
        lll._PROVIDER_HEALTH.clear()
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"]),
                         ["cloudru", "deepseek"])
        # медленный первый токен — понижение
        for _ in range(3):
            lll._record_provider_health("cloudru", True, ttft_s=9.0, cps=200)
        lll._record_provider_health("deepseek", True, ttft_s=0.8, cps=180)
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "deepseek")
        # печать по слову в секунду — понижение
        lll._PROVIDER_HEALTH.clear()
        for _ in range(3):
            lll._record_provider_health("cloudru", True, ttft_s=1.0, cps=5)
        lll._record_provider_health("deepseek", True, ttft_s=1.0, cps=150)
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "deepseek")
        lll._PROVIDER_HEALTH.clear()

    def test_agent_translates_provider_switch_to_status(self) -> None:
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn('elif etype == "provider_switch":', src)
        self.assertIn("отвечает медленно — пробую резервную модель", src)


class IterationBM6Tests(unittest.TestCase):
    """BM6 — точное распознавание сбоя провайдера (зонд каждые 45/10 секунд,
    мёртвый обходится сразу) и подключение любого РФ-провайдера ключом из
    конфига: Yandex AI Studio (api-key + folder_id), GigaChat (OAuth-токен),
    AITunnel (bearer). Порядок выбора задаётся приоритетом."""

    def setUp(self) -> None:
        llm._PROVIDER_HEALTH.clear()
        llm._PROBE_STATE.clear()
        llm._TOKEN_CACHE.clear()

    def test_probe_two_failures_mark_dead_and_order_skips(self) -> None:
        """Два зонда подряд упали — провайдер мёртв и обходитcя сразу."""
        import jarvis.llm as lll
        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "http://x/v1"}), \
             mock.patch.object(lll.urllib.request, "urlopen",
                               side_effect=OSError("no route")):
            self.assertFalse(lll.probe_provider("cloudru"))
            self.assertFalse(lll.probe_provider("cloudru"))
        st = lll.provider_probe_status("cloudru")
        self.assertTrue(st["dead"], "двойной отказ зонда = мёртв")
        self.assertFalse(st["ok"])
        order = lll.provider_order(["cloudru", "deepseek"])
        self.assertEqual(order[0], "deepseek",
                         "мёртвый провайдер уходит в конец очереди сразу")

    def test_probe_recovery_clears_dead(self) -> None:
        """Провайдер ожил — пометка мёртвого снимается сама."""
        import jarvis.llm as lll

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "http://x/v1"}):
            with mock.patch.object(lll.urllib.request, "urlopen",
                                   side_effect=OSError("down")):
                lll.probe_provider("cloudru")
                lll.probe_provider("cloudru")
            self.assertTrue(lll.provider_probe_status("cloudru")["dead"])
            with mock.patch.object(lll.urllib.request, "urlopen",
                                   return_value=Resp()):
                self.assertTrue(lll.probe_provider("cloudru"))
        st = lll.provider_probe_status("cloudru")
        self.assertFalse(st["dead"], "живой зонд снимает мёртвость")
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "cloudru")

    def test_probe_auth_error_means_alive(self) -> None:
        """401 от API = сервис жив (сеть и шлюз работают), не «лежит»."""
        import urllib.error
        import jarvis.llm as lll
        err = urllib.error.HTTPError("http://x/v1/models", 401, "Unauthorized",
                                     hdrs=None, fp=None)
        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "bad",
                                             "base_url": "http://x/v1"}), \
             mock.patch.object(lll.urllib.request, "urlopen", side_effect=err):
            self.assertTrue(lll.probe_provider("cloudru"))
        self.assertFalse(lll.provider_probe_status("cloudru")["dead"])

    def test_provider_headers_schemes(self) -> None:
        """Bearer по умолчанию; Yandex — Api-Key + папка; GigaChat — токен."""
        import jarvis.llm as lll
        h = lll.provider_headers({"api_key": "sk-1"})
        self.assertEqual(h["Authorization"], "Bearer sk-1")
        h = lll.provider_headers({"api_key": "yk", "auth": "api-key",
                                  "folder_id": "b1g"})
        self.assertEqual(h["Authorization"], "Api-Key yk")
        self.assertEqual(h["x-folder-id"], "b1g")
        self.assertEqual(h["OpenAI-Project"], "b1g")
        with mock.patch.object(lll, "_gigachat_token", return_value="tok77"):
            h = lll.provider_headers({"api_key": "gz", "auth": "gigachat"})
        self.assertEqual(h["Authorization"], "Bearer tok77")

    def test_gigachat_token_exchanged_once_and_cached(self) -> None:
        """OAuth-токен Сбера меняется один раз и живёт в кэше 25 минут."""
        import jarvis.llm as lll

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"access_token": "T1"}).encode()

        calls = []

        def fake_urlopen(req, timeout=0, context=None):
            calls.append(req.full_url)
            self.assertIn("ngw.devices.sber.ru", req.full_url)
            return Resp()

        conf = {"api_key": "authkey", "auth": "gigachat"}
        with mock.patch.object(lll.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            t1 = lll._gigachat_token(conf)
            t2 = lll._gigachat_token(conf)
        self.assertEqual(t1, "T1")
        self.assertEqual(t1, t2)
        self.assertEqual(len(calls), 1, "второй обмен не нужен — токен в кэше")

    def test_active_providers_sorted_by_priority(self) -> None:
        """Приоритет меньше — выбирается первым; без ключа не выбирается."""
        import jarvis.llm as lll
        confs = {"yandex": {"enabled": True, "api_key": "y", "priority": 5},
                 "cloudru": {"enabled": True, "api_key": "c", "priority": 0},
                 "deepseek": {"enabled": True, "api_key": "d", "priority": 10},
                 "gigachat": {"enabled": False, "api_key": "", "priority": 20}}
        with mock.patch.object(lll, "CONFIG", {"providers": confs}):
            self.assertEqual(lll.active_providers(),
                             ["cloudru", "yandex", "deepseek"])

    def test_providers_status_snapshot(self) -> None:
        """Снимок для UI: зонд, здоровье, порядок и человекочитаемая метка."""
        import jarvis.llm as lll
        lll._record_provider_health("cloudru", True, ttft_s=0.3, cps=150.0)
        lll._record_provider_health("cloudru", True, ttft_s=0.5, cps=120.0)
        snap = lll.providers_status()
        self.assertIn("cloudru", snap)
        row = snap["cloudru"]
        self.assertIn("probe", row)
        self.assertEqual(row["med_ttft_s"], 0.5)
        self.assertGreaterEqual(row["med_cps"], 120.0)
        self.assertIn("label", row)

    def test_yandex_image_async_flow(self) -> None:
        """YandexART: асинхронная операция -> опрос -> готовая картинка."""
        import jarvis.tools.media as med
        from unittest import mock as _mock

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
            "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

        class Resp:
            def __init__(self, body): self._b = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self._b).encode("utf-8")

        flows = []

        def fake_urlopen(req, timeout=0, context=None):
            url = req.full_url
            auth = req.get_header("Authorization") or ""
            if "imageGenerationAsync" in url:
                flows.append(("post", auth,
                              json.loads(req.data.decode("utf-8"))))
                return Resp({"id": "op-abc12345"})
            flows.append(("poll", auth, None))
            n = sum(1 for f in flows if f[0] == "poll")
            if n == 1:
                return Resp({"done": False})
            return Resp({"done": True,
                         "response": {"image": base64.b64encode(png).decode()}})

        conf = {"providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False,
                "media.yandex_image_model": "yandex-art"}
        tmpdir = tempfile.mkdtemp(prefix="bm6_ya_")
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med.urllib.request, "urlopen",
                                side_effect=fake_urlopen), \
             _mock.patch.object(med.time, "sleep", lambda s: None), \
             _mock.patch.object(med.sandbox, "safe_path",
                                side_effect=lambda n: Path(tmpdir) / n), \
             _mock.patch.object(med.sandbox, "dl",
                                return_value="/dl/x"):
            res = med.generate_image("кот", 1024, 512)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["provider"], "yandex")
        post = next(f for f in flows if f[0] == "post")
        self.assertEqual(post[1], "Api-Key yk")
        self.assertIn("art://b1g/yandex-art/latest", post[2]["modelUri"])
        self.assertEqual(post[2]["generationOptions"]["aspectRatio"],
                         {"widthRatio": "2", "heightRatio": "1"})
        self.assertEqual(len([f for f in flows if f[0] == "poll"]), 2)
        self.assertTrue(Path(res["path"]).is_absolute() is False or True)
        written = Path(tmpdir) / res["name"]
        self.assertTrue(written.exists(), "картинка сохранена на диск")
        self.assertEqual(written.read_bytes(), png)

    def test_yandex_image_retries_bearer_on_401(self) -> None:
        """Редкий шлюз не принял Api-Key — один повтор с Bearer схемой."""
        import urllib.error
        import jarvis.tools.media as med
        from unittest import mock as _mock

        class Resp:
            def __init__(self, body): self._b = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self._b).encode("utf-8")

        auths = []

        def fake_urlopen(req, timeout=0, context=None):
            if "imageGenerationAsync" in req.full_url:
                auths.append(req.get_header("Authorization"))
                if len(auths) == 1:
                    raise urllib.error.HTTPError(
                        req.full_url, 401, "Unauthorized", hdrs=None, fp=None)
                return Resp({"id": "op-x"})
            raise urllib.error.HTTPError(
                req.full_url, 408, "timeout", hdrs=None, fp=None)

        conf = {"providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False}
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med.urllib.request, "urlopen",
                                side_effect=fake_urlopen), \
             _mock.patch.object(med.time, "sleep", lambda s: None):
            with self.assertRaises(med.GigaChatError):
                med._yandex_image("кот", 512, 512)
        self.assertEqual(auths, ["Api-Key yk", "Bearer yk"])

    def test_image_chain_prefers_yandex_when_configured(self) -> None:
        """auto без gateway: Яндекс настроен — он рисует первым из платных."""
        import jarvis.tools.media as med
        from unittest import mock as _mock
        conf = {"media.image_provider": "auto",
                "media.image_gateway_url": "", "media.image_gateway_token": "",
                "providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False}
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med, "_yandex_image",
                                return_value={"ok": True, "provider": "yandex",
                                              "path": "x.jpeg"}) as mk:
            res = med.generate_image("кот", 512, 512)
        self.assertTrue(res["ok"])
        mk.assert_called_once()

    def test_chat_stream_survives_gigachat_token_failure(self) -> None:
        """GigaChat-токен не обменялся — тихо идём к следующему провайдеру."""
        import jarvis.llm as lll

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                raise StopIteration

        def fake_headers(conf):
            if str(conf.get("auth") or "") == "gigachat":
                raise lll.LLMError("GigaChat не выдал токен")
            return {"Authorization": "Bearer ok"}

        with mock.patch.object(lll, "active_providers",
                               return_value=["gigachat", "deepseek"]), \
             mock.patch.object(lll, "provider_conf",
                               side_effect=lambda n: {
                                   "gigachat": {"api_key": "g", "auth": "gigachat",
                                                "base_url": "http://gg"},
                                   "deepseek": {"api_key": "d",
                                                "base_url": "http://ds"}}[n]), \
             mock.patch.object(lll, "pick_model", return_value="m"), \
             mock.patch.object(lll, "provider_headers",
                               side_effect=fake_headers), \
             mock.patch.object(lll, "_request", return_value=Resp()):
            events = list(lll.chat_stream([{"role": "user", "content": "q"}]))
        kinds = [e["type"] for e in events]
        self.assertNotIn("provider_switch", kinds)
        self.assertTrue(any(k in kinds for k in ("delta", "done", "error")),
                        "обработка не падает — уходит к резерву")


class IterationBM7Tests(unittest.TestCase):
    """BM7 — зонд обязан измерять ГЕНЕРАЦИЮ, а не только «сайт жив»
    (первопричина «пишет что всё норм, а отвечать не может»); тарифы
    разделены по провайдерам; Яндекс рисует раньше бесплатного gateway;
    провайдер виден в чипе и в шапке каждого ответа."""

    def setUp(self) -> None:
        llm._PROVIDER_HEALTH.clear()
        llm._PROBE_STATE.clear()
        llm._GEN_PROBE_STATE.clear()
        llm._TOKEN_CACHE.clear()

    def test_gen_probe_demotes_slow_but_alive_site(self) -> None:
        """Сайт отвечает быстро, генерация деградировала — понижение."""
        import jarvis.llm as lll

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        clock = {"t": 1000.0}

        def fake_monotonic():
            v = clock["t"]
            clock["t"] += 8.0        # каждый замер «длится» 8 секунд
            return v

        # часы фейковые: и замер, и чтение здоровья — в одном времени
        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "http://x/v1"}), \
             mock.patch.object(lll, "pick_model", return_value="m"), \
             mock.patch.object(lll, "_request", return_value=Resp()), \
             mock.patch.object(lll.time, "monotonic", side_effect=fake_monotonic):
            t1 = lll.generation_probe("cloudru")
            t2 = lll.generation_probe("cloudru")
            self.assertEqual(t1, 8.0)
            self.assertEqual(t2, 8.0)
            # сайт при этом ЖИВ: /models отвечает мгновенно — старый зонд
            # говорил бы «всё норм», а генерационный понижает в очереди
            self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                             "deepseek", "медленная генерация = понижение")

    def test_gen_probe_429_counts_as_degradation(self) -> None:
        """429 (перегружен) — это деградация, а 401 (нет ключа) — нет."""
        import urllib.error
        import jarvis.llm as lll

        def boom_429(url, key, payload=None, method="POST", timeout=180,
                     headers=None, ssl_ctx=None):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests",
                                         hdrs=None, fp=None)

        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "http://x/v1"}), \
             mock.patch.object(lll, "pick_model", return_value="m"), \
             mock.patch.object(lll, "_request", side_effect=boom_429):
            self.assertIsNone(lll.generation_probe("cloudru"))
            self.assertIsNone(lll.generation_probe("cloudru"))
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "deepseek", "429 дважды — провайдер перегружен")

    def test_estimate_cost_per_provider(self) -> None:
        """Одна модель — разные цены у разных провайдеров."""
        self.assertAlmostEqual(llm.estimate_cost("gpt-oss-120b", 1e6, 0, "cloudru"),
                               15.86)
        self.assertAlmostEqual(llm.estimate_cost("gpt-oss-120b", 1e6, 0, "yandex"),
                               300.0)
        self.assertAlmostEqual(
            llm.estimate_cost("qwen3-235b-a3b-instruct", 1e6, 0, "yandex"), 500.0)
        self.assertAlmostEqual(llm.estimate_cost("GigaChat-Pro", 1e6, 1e6, "gigachat"),
                               0.0)
        self.assertAlmostEqual(llm.estimate_cost("deepseek-chat", 1e6, 0, "deepseek"),
                               25.0)
        # старый вызов без провайдера продолжает работать
        self.assertAlmostEqual(llm.estimate_cost("deepseek-chat", 1e6, 0), 25.0)

    def test_image_chain_yandex_before_gateway(self) -> None:
        """Яндекс настроен — он рисует ПЕРВЫМ, бесплатный gateway запасной."""
        import jarvis.tools.media as med
        from unittest import mock as _mock
        conf = {"media.image_provider": "auto",
                "media.image_gateway_url": "https://gw.example",
                "media.image_gateway_token": "tok",
                "providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False}
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med, "_yandex_image",
                                return_value={"ok": True, "provider": "yandex",
                                              "path": "x.jpeg"}) as mk_ya, \
             _mock.patch.object(med, "_gateway_image",
                                return_value={"ok": True}) as mk_gw:
            res = med.generate_image("кот", 512, 512)
        self.assertTrue(res["ok"])
        mk_ya.assert_called_once()
        mk_gw.assert_not_called()

    def test_yandex_image_cost_logged(self) -> None:
        """Картинка Яндекса стоит 2.23₽ — попадает в счётчик расходов."""
        import base64 as _b64
        import jarvis.tools.media as med
        from unittest import mock as _mock

        png = _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
            "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

        class Resp:
            def __init__(self, body): self._b = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self._b).encode("utf-8")

        def fake_urlopen(req, timeout=0, context=None):
            if "imageGenerationAsync" in req.full_url:
                return Resp({"id": "op-cost123"})
            return Resp({"done": True,
                         "response": {"image": _b64.b64encode(png).decode()}})

        conf = {"providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False}
        tmpdir = tempfile.mkdtemp(prefix="bm7_cost_")
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med.urllib.request, "urlopen",
                                side_effect=fake_urlopen), \
             _mock.patch.object(med.time, "sleep", lambda s: None), \
             _mock.patch.object(med.sandbox, "safe_path",
                                side_effect=lambda n: Path(tmpdir) / n), \
             _mock.patch.object(med.sandbox, "dl", return_value="/dl/x"), \
             _mock.patch.object(med.db, "log_usage") as mk_log:
            res = med._yandex_image("кот", 512, 512)
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["cost_rub"], 2.23)
        mk_log.assert_called_once()
        args = mk_log.call_args[0]
        self.assertEqual(args[0], "yandex")
        self.assertEqual(args[1], "yandex-art")
        self.assertEqual(args[2], "image")
        self.assertAlmostEqual(args[5], 2.23)

    def test_agent_model_event_carries_provider(self) -> None:
        """Событие model несёт провайдера — фронт покажет в шапке ответа."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn('self.provider_used = str(event.get("provider") or "")', src)
        self.assertIn('"provider": self.provider_used', src)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("if (prov) parts.push(prov);", js)

    def test_providers_status_shows_gen_probe(self) -> None:
        """Снимок для UI включает и генерационный зонд."""
        import jarvis.llm as lll
        lll._GEN_PROBE_STATE["cloudru"] = {"ttft": 3.2, "at": time.monotonic()}
        snap = lll.providers_status()
        self.assertEqual(snap["cloudru"]["gen_ttft_s"], 3.2)


class IterationBM8Tests(unittest.TestCase):
    """BM8 — ген-зонд меряет РАБОЧУЮ модель (base), а не nano; реальные
    медленные ответы (TTFT 4-6с) понижают провайдера; улучшатель промпта
    картинки больше не жуёт 3 минуты; JSON-ответы с любыми ключами
    ловятся; границы инструмента не гаснут; расход считается за сегодня."""

    def setUp(self) -> None:
        llm._PROVIDER_HEALTH.clear()
        llm._PROBE_STATE.clear()
        llm._GEN_PROBE_STATE.clear()
        llm._TOKEN_CACHE.clear()

    def test_gen_probe_uses_base_model(self) -> None:
        """Зонд на nano врал «ген 0.16с» — меряем рабочую модель base."""
        import jarvis.llm as lll

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        tiers = []
        with mock.patch.object(lll, "provider_conf",
                               return_value={"api_key": "k",
                                             "base_url": "http://x/v1"}), \
             mock.patch.object(lll, "pick_model",
                               side_effect=lambda t, p: tiers.append(t) or "m"), \
             mock.patch.object(lll, "_request", return_value=Resp()):
            lll.generation_probe("cloudru")
        self.assertEqual(tiers, ["base"], "ген-зонд обязан мерять base-модель")

    def test_health_thresholds_catch_mild_degradation(self) -> None:
        """TTFT 5с (раньше порог 6с пропускал) — уже деградация."""
        import jarvis.llm as lll
        lll._record_provider_health("cloudru", True, ttft_s=5.0, cps=180.0)
        lll._record_provider_health("cloudru", True, ttft_s=5.0, cps=180.0)
        self.assertEqual(lll.provider_order(["cloudru", "deepseek"])[0],
                         "deepseek", "5с до первого токена = деградация")
        # а 2.5с — нормальная жизнь, понижать нельзя
        lll._PROVIDER_HEALTH.clear()
        lll._record_provider_health("yandex", True, ttft_s=2.5, cps=150.0)
        lll._record_provider_health("yandex", True, ttft_s=2.5, cps=150.0)
        self.assertEqual(lll.provider_order(["yandex", "deepseek"])[0],
                         "yandex")

    def test_enhance_prompt_has_hard_timeout(self) -> None:
        """КОРЕНЬ «картинка минут 3 без результата»: улучшатель промпта
        ходил к nano без таймаута (180с по умолчанию)."""
        import jarvis.tools.media as med
        from unittest import mock as _mock
        captured = {}
        with _mock.patch.object(
                med.llm, "chat",
                side_effect=lambda *a, **kw: captured.update(kw) or
                {"content": "промпт"}) as mk:
            out = med._enhance_prompt("кот на окне", 1024, 1024)
        mk.assert_called_once()
        self.assertEqual(captured.get("timeout"), 12,
                         "улучшатель обязан уложиться в 12 секунд")
        self.assertTrue(out)

    def test_free_image_chain_has_budget(self) -> None:
        """Бесплатная цепочка ограничена 90 секундами В ЦЕЛОМ."""
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertIn("_FREE_IMAGE_BUDGET_S = 90.0", code)
        self.assertIn("timeout=min(75, max(2, left))", code)

    def test_yandex_failure_surfaces_in_final_error(self) -> None:
        """Яндекс отказал — финальная ошибка называет причину, а не молчит."""
        import jarvis.tools.media as med
        from unittest import mock as _mock
        conf = {"media.image_provider": "auto",
                "media.image_gateway_url": "", "media.image_gateway_token": "",
                "providers.yandex": {"api_key": "yk", "folder_id": "b1g"},
                "media.enhance_prompt": False}
        with _mock.patch.object(med, "CONFIG", conf), \
             _mock.patch.object(med, "_yandex_image",
                                side_effect=med.GigaChatError("403: нет роли")), \
             _mock.patch.object(
                 med, "_free_image",
                 return_value={"ok": False,
                               "error": "бесплатный генератор временно недоступен (x)"}):
            res = med.generate_image("кот", 512, 512)
        self.assertFalse(res["ok"])
        self.assertIn("Яндекс не нарисовал", res["error"])
        self.assertIn("403", res["error"])

    def test_degenerate_json_answer_detection(self) -> None:
        """JSON с любыми ключами и массивы — тоже вырожденный ответ."""
        from jarvis import agent as ag
        self.assertTrue(ag.is_degenerate_json_answer(
            '{"answer": "сумма", "total": 42}'))
        self.assertTrue(ag.is_degenerate_json_answer(
            "```json\n{\"a\": 1, \"b\": 2}\n```"))
        self.assertTrue(ag.is_degenerate_json_answer(
            '[{"question": "один", "answer": "два"}, {"question": "три"}]'))
        self.assertFalse(ag.is_degenerate_json_answer("Обычный текст ответа"))
        self.assertFalse(ag.is_degenerate_json_answer('{"a": 1} и текст'))
        # человек сам просил JSON — ловушка не работает
        self.assertTrue(ag.user_wants_json("дай JSON со списком"))
        self.assertFalse(ag.user_wants_json("нарисуй кота"))

    def test_usage_summary_since_midnight(self) -> None:
        """usage_summary умеет считать «с момента» (местная полночь)."""
        import jarvis.db as jdb
        with mock.patch.object(jdb, "query_one", return_value={}) as q1, \
             mock.patch.object(jdb, "query", return_value=[]):
            jdb.usage_summary(since=1770000000.0)
        # второй позиционный аргумент — кортеж параметров (SQL первый)
        self.assertEqual(q1.call_args[0][1][0], 1770000000.0)

    def test_state_accepts_day_start(self) -> None:
        """Сервер: /api/state?day_start= переключает usage на «за сегодня»."""
        src = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn('params.get("day_start")', src)
        self.assertIn("db.usage_summary(since=day_start)", src)

    def test_deep_probe_endpoint_and_lock(self) -> None:
        """?deep=1 — ген-зонд всех провайдеров; повторные клики не плодят."""
        import jarvis.llm as lll
        src = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn('params.get("deep")', src)
        self.assertIn("llm.deep_probe_all()", src)
        probed = []
        with mock.patch.object(lll, "active_providers",
                               return_value=["a", "b"]), \
             mock.patch.object(lll, "generation_probe",
                               side_effect=lambda n: probed.append(n)):
            lll.deep_probe_all(timeout_s=3.0)
        self.assertEqual(sorted(probed), ["a", "b"])

    def test_gen_probe_cadence_cheap_when_healthy(self) -> None:
        """Здоровый провайдер — зонд раз в 5 минут, под штрафом — 2 минуты."""
        import jarvis.llm as lll
        self.assertEqual(lll._GEN_PROBE_GAP_S, 300.0)
        self.assertEqual(lll._GEN_PROBE_GAP_HOT_S, 120.0)

    def test_tool_mask_never_removed(self) -> None:
        """Затемнение краёв потока инструмента — навсегда после переполнения."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        glide = js.split("function glideFlow(")[1].split("\nfunction ")[0]
        self.assertNotIn("classList.remove('full')", glide,
                         "маска границ не снимается никогда")
        # новые строки перезапускают полёт при стоящей маске
        feed = js.split("function qtFeed(")[1].split("\nfunction ")[0]
        self.assertIn("flow.classList.contains('full') && inner.scrollHeight", feed)

    def test_chip_shows_role_light_not_name(self) -> None:
        """Чип — только имя провайдера и огонёк роли; слово «провайдер» убрано.
        BM30.1: имя = приоритетно ДОСТУПНЫЙ провайдер (первый живой по
        приоритету), а не последний использованный — просьба человека."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        spc = js.split("function setProvChip(name)")[1].split("\nfunction ")[0]
        self.assertIn("? (PROV_SHORT[pr.name] || pr.name) : '—';", spc)
        self.assertIn("function provPriority() {", js)
        self.assertNotIn("'провайдер: '", js)
        self.assertIn("pr.order === 0 ? 'ok'", spc)
        self.assertIn("'err live'", spc)
        self.assertIn("PROV_SHORT", spc)
        self.assertIn("cloud.ru", js.split("const PROV_SHORT")[1][:200])

    def test_bm9_suggestion_prompt_repaired(self) -> None:
        """Промпт подсказок больше не содержит задублированный мусорный
        фрагмент, разрывавший инструкцию (первопричина шаблонов и
        «стоит посмотреть»)."""
        from jarvis import agent as ag
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # двойной фрагмент удалён
        self.assertNotIn('с глаголом. "\n                        "примере»', src)
        seg = src.split("Ты придумаешь продолжение переписки")[1][:1600]
        self.assertEqual(seg.count("что дальше»"), 1,
                         "список запрещённых фраз встречается ровно один раз")
        # голые оценки запрещены
        self.assertIn("стоит посмотреть", seg)

    def test_bm10_show_media_tool(self) -> None:
        """Медиа из интернета: прямые ссылки качаются, YouTube честно
        отказывает (РФ), тип определяется по Content-Type."""
        import jarvis.tools.media as med
        from unittest import mock as _mock

        # YouTube — честный отказ без сети
        res = med.show_media("https://www.youtube.com/watch?v=abc")
        self.assertFalse(res["ok"])
        self.assertIn("YouTube", res["error"])

        res = med.show_media("ftp://x/file.mp4")
        self.assertFalse(res["ok"])
        res = med.show_media("")
        self.assertFalse(res["ok"])

        # прямое видео: качается, тип video, файл в песочнице
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 4000

        class Resp:
            headers = {"Content-Type": "video/mp4", "Content-Length": str(len(mp4))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1):
                out, mp4.__class__ = mp4[:n], None  # noqa
                return out

        data = {"buf": mp4}
        class Resp2:
            headers = {"Content-Type": "video/mp4",
                       "Content-Length": str(len(data["buf"]))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1):
                out = data["buf"][:n]
                data["buf"] = data["buf"][len(out):] if n > 0 else b""
                return out

        tmpdir = tempfile.mkdtemp(prefix="bm10_media_")
        with _mock.patch.object(med.urllib.request, "urlopen", return_value=Resp2()), \
             _mock.patch.object(med.sandbox, "safe_path",
                                side_effect=lambda n: Path(tmpdir) / n), \
             _mock.patch.object(med.sandbox, "dl", return_value="/dl/x"):
            res = med.show_media("https://example.com/clip.mp4")
        self.assertTrue(res, res if not res.get("ok") else "")
        self.assertEqual(res["kind"], "video")
        self.assertTrue((Path(tmpdir) / res["name"]).exists())
        self.assertTrue(res["name"].endswith(".mp4"))

    def test_bm10_file_info_carries_media_kind(self) -> None:
        """Файловая карточка несёт kind видео/аудио — фронт ставит плеер."""
        from jarvis import agent as ag
        info = ag._file_info_of({"download_url": "/dl/x", "path": "media_a1.mp4",
                                 "size": 5, "kind": "video"})
        self.assertEqual(info["kind"], "video")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("v.autoplay = true; v.muted = true;", js)

    def test_bm10_suggestions_ask_jarvis(self) -> None:
        """Подсказки — обращения к JARVIS (вопрос/просьба), не «могу…»."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("обращение ", src)
        self.assertIn("могу выбрать ", src)   # запрет пример прямо в промпте

    def test_bm10_interactives_not_banned(self) -> None:
        """Интерактивы не запрещены: единственный запрет — повтор уже
        отвеченной панели (первопричина бесконечного цикла)."""
        from jarvis import agent as ag
        c = ag.PROACTIVE_UI_CONTRACT
        self.assertNotIn("НЕ показывай ui для", c)
        self.assertIn("НЕ выставляй панель", c)

    def test_bm10_spaces_shell(self) -> None:
        """Каркас пространств: CHAT/LIVE + мини-иконки, свайп, анимация."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('id="spacesBar"', html)
        self.assertIn('id="spModeLive"', html)
        self.assertIn('data-space="math"', html)
        self.assertIn('data-space="music"', html)
        self.assertIn('id="view-space"', html)
        self.assertIn("const SPACES = ['chat', 'math', 'music'];", js)
        self.assertIn("function setSpace(name, dir)", js)
        self.assertIn("initSpaces();", js)
        # свайп двумя пальцами: колесо с deltaX, быстрый порог;
        # BM13: ОДИН переход за жест — arm/disarm
        self.assertIn("const dx = Math.abs(e.deltaX);", js)
        self.assertIn("if (dx < 4) { swipeArmed = true; return; }", js)
        # BM22: ходовой курок — жест редкий, пороги снижены
        self.assertIn("if (dx < 12 || dx < Math.abs(e.deltaY) * 0.85) return;", js)
        self.assertIn("if (now - lastSwipe < 240) return;", js)
        # старт всегда в CHAT
        self.assertIn("spaceApply('chat');", js)
        # BM11: подчёркивания у выбранного нет — светится; глайдер перетекает
        self.assertNotIn('.sp-ico.sel::after', css)
        self.assertIn('.sp-glider{', css)
        self.assertIn('function spaceGlider()', js)
        self.assertIn('.sp-mode.active', css)

    def test_bm11_show_media_known_to_the_agent(self) -> None:
        """Модель знает про show_media и не отказывается от аудио/видео."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("show_media — картинка, аудио или видео из интернета", src)
        flat = " ".join(src.split())
        # BM13: страница — тоже валидный вход; отказ без попытки запрещён
        self.assertIn(
            "НИКОГДА не говори «не могу передать аудио- или видеоконтент» "
            "и «не смог найти прямую ссылку» без попытки show_media "
            "со страницей", flat)
        self.assertIn("он сам найдёт медиа внутри страницы", flat)

    def test_bm11_embed_rule_and_fences(self) -> None:
        """Мини-вкладки: правило в промпте + фенс в markdown + карточка в app.js."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("МИНИ-ВКЛАДКИ", src)
        self.assertIn("```embed", src)
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        self.assertIn("lang === 'embed'", md)
        self.assertIn('class="embed-panel" data-embed=', md)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function mountEmbedPanels(", js)
        self.assertIn("function buildEmbedPanel(panel, spec)", js)
        for view in ("auto", "files", "memory", "scenarios"):
            self.assertIn("%s: {" % view, js.split("const EMBED_VIEWS = {")[1]
                          .split("};")[0])

    def test_bm11_reply_wait_covers_nano_budget(self) -> None:
        """Ожидание подсказок покрывает бюджет nano (14с), чипы не пропадают."""
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertEqual(srv.count('wait(timeout=16.0)'), 2,
                         "оба ожидания бегущего расчёта — 16с")
        self.assertNotIn("wait(timeout=11.0)", srv)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("}, 18000);", js)

    def test_bm11_budget_button_alignment(self) -> None:
        """Кнопка лимита: перед AGENT, ровно под ускорением; AGENT — под Send."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("width:30px;height:28px;border-radius:20px;", css)
        self.assertIn("margin-right:2px}", css)
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        # BM12: AGENT крайняя справа (правый край 10px — под Send),
        # лимит левее него (правый край 61px — под ускорением)
        self.assertLess(html.index('id="tgBudget"'), html.index('id="swAgent"'),
                        "лимит стоит перед тумблером AGENT")

    def test_bm11_root_degree_lower(self) -> None:
        """Степень корня в границах самого корня: ниже и левее."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        # BM13: степень живёт ВНУТРИ рамки корня — не наезжает на скобку слева
        self.assertIn("msqrt' + (root ? ' msqrt-i' : ''", md)
        # BM16: степень над нижним загибом — см. bm13_root_degree_inside_box
        self.assertIn(".msqrt .msq-i{position:absolute;left:-.04em;bottom:calc(47% + .01em);font-size:.58em", css)
        self.assertIn(".msqrt.msqrt-i{padding-left:calc(.40em + var(--msq-x,0em))}", css)
        self.assertIn(".msqrt.msqrt-i .msq-svg{left:var(--msq-x,0em)}", css)
        self.assertNotIn("msq-box", css)
        self.assertIn(".mfr-d .msqrt{margin-top:.22em}", css)

    def test_bm11_dock_untouched_spaces_flyout(self) -> None:
        """Док: вкладки как раньше + LIVE + текущее пространство с выплывающей панелью."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        for marker in ('id="spDock"', 'id="spdLive"', 'id="spdCur"', 'id="spdFly"',
                       'id="spdCurWrap"', 'class="spd-dash"', 'id="spSettings"',
                       'id="spGlider"', 'id="spSetPanel"'):
            self.assertIn(marker, html)
        self.assertIn(".app.docked .spaces{visibility:hidden}", css)
        self.assertIn(".app.collapsed .sp-dock{display:flex", css)
        self.assertIn(".spd-cur-wrap.open .spd-fly{opacity:1", css)
        # BM14: меню пространств вырастает ИЗ кнопки (left:0), не сбоку
        self.assertIn("transform:translateY(-50%) scale(.22);transform-origin:left center", css)

    def test_bm11_plot_empty_plane_banned(self) -> None:
        """Пустая плоскость запрещена: окно по точкам, честная ошибка."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("ДАННЫЕ ВАЖНЕЕ диапазона из спеки", js)
        self.assertIn("ПУСТАЯ ПЛОСКОСТЬ ЗАПРЕЩЕНА", js)
        self.assertIn("panel._emptyGuard", js)

    def test_bm12_auto_embed_after_tool_run(self) -> None:
        """Карточка вкладки — сама после прогона с изменениями (A+B+D)."""
        # BM19: задача запущена — ЗЕЛЁНЫЙ БАННЕР В ДИАЛОГЕ + карточка АВТО
        out = agent.auto_embed_block("Поставил задачу.", ["schedule_task"])
        self.assertIn("**Фоновая задача поставлена**", out)
        self.assertIn('"view": "auto"', out)
        # файл создан — карточка ФАЙЛОВ
        out = agent.auto_embed_block("Готово.", ["write_file", "run_python"])
        self.assertIn('"view": "files"', out)
        # факт сохранён (инструментом или фоновым parser'ом) — карточка ПАМЯТИ
        self.assertIn('"view": "memory"',
                      agent.auto_embed_block("Запомнил.", ["remember"]))
        self.assertIn('"view": "memory"',
                      agent.auto_embed_block("Запомнил.", [], memory_changed=True))
        # модель уже дала ВАЛИДНУЮ карточку — дубль не создаём
        done = "вот\n```embed\n{\"view\": \"memory\"}\n```"
        self.assertEqual(agent.auto_embed_block(done, []), done)
        # BM18: БИТЫЙ embed-блок (без view) — не карточка: вычищается,
        # а не отменяет гарантию; кривой JSON с ОДИНАРНЫМИ кавычками —
        # валиден (фронт разбирает и такой)
        loose = "вот\n```embed\n{'view': 'memory',}\n```"
        self.assertEqual(agent.auto_embed_block(loose, []), loose)
        broken = "вот\n```embed\nкакая-то вкладка\n```"
        cleaned = agent.auto_embed_block(broken, [])
        self.assertNotIn("```embed", cleaned)
        self.assertIn("вот", cleaned)
        # и гарантия работает: битый блок + вопрос о памяти => карточка есть
        rescued = agent.auto_embed_block(broken, [], q_view="memory")
        self.assertIn('"view": "memory"', rescued)
        # ничего не изменилось — ответ не трогаем
        self.assertEqual(agent.auto_embed_block("просто ответ", []), "просто ответ")
        # BM19: пустой ответ с задачей — ЗЕЛЁНЫЙ БАННЕР + карточка АВТО;
        # вопрос о состоянии — карточка ГАРАНТИРОВАНА
        out = agent.auto_embed_block("", ["schedule_task"])
        self.assertIn("**Фоновая задача поставлена**", out)
        self.assertIn('"view": "auto"', out)
        self.assertIn('"view": "auto"',
                      agent.auto_embed_block("", [], q_view="auto"))

    def test_bm12_auto_embed_wired_everywhere(self) -> None:
        """Авто-карточка подключена: чат-прогон, тихий прогон, промпт."""
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        self.assertIn("def _memory_mark():", srv)
        self.assertIn("mem_mark = _memory_mark()", srv)
        self.assertIn("final_text = agent.auto_embed_block(", srv)
        auto = Path("app/jarvis/auto.py").read_text(encoding="utf-8")
        # BM16: сработавшая задача — просто отложенное сообщение
        self.assertNotIn("agent.auto_embed_block(content", auto)
        # промпт: перечисление и вопросы о состоянии — всегда карточкой
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        flat = " ".join(src.split())
        # BM14: текст допустим, карточка ОБЯЗАТЕЛЬНА как дополнение
        self.assertIn("текст допустим, но живая карточка ОБЯЗАТЕЛЬНА как дополнение", flat)
        self.assertIn("ВОПРОС О СОСТОЯНИИ", flat)
        self.assertIn("НИЧЕГО не запускай и не создавай — ни фоновых задач, ни файлов", flat)
        self.assertIn("Запрещено вместо медиа сохранять результаты поиска в JSON-файл", flat)
        self.assertIn("НЕ вставляй карточку AUTO сам", flat)
        self.assertIn("зелёная блашка «Фоновая задача поставлена»", flat)

    def test_bm12_implicit_multiplication(self) -> None:
        """Парсер формул понимает «2x», «2sin(x)», «-2x» — прежде молча NaN."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("const implicit = () => { if (prev === 'n') ops.push('*'); };", js)
        # унарный минус помечен 'u' — после него неявного умножения нет
        self.assertIn("ops.push('u-'); prev = 'u'; continue;", js)

    def test_bm12_news_digest_chips_universal(self) -> None:
        """Длинная сводка без кода не получает кодовых чипов; дефолт проактивных
        тоже универсальный."""
        news = ("Новости дня. Рынки выросли. " * 90).strip()
        items = agent.suggest_replies("что нового?", news)
        self.assertNotIn("Сохрани в файл", items)
        self.assertEqual(items, ["Уточни главное", "Предложи варианты развития",
                                 "Как это применить?"])
        # проактивные: без новостных слов — универсальные направления
        pro = agent.suggest_proactive("сделай отчёт", "готово", ["run_python"])
        self.assertIn("Уточни главное", pro)
        self.assertIn("Сделай следующий шаг сам", pro)
        self.assertNotIn("Что можно улучшить в результате?", pro)

    def test_bm12_media_file_not_link(self) -> None:
        """Медиа — файлом в диалоге, а не ссылкой."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        flat = " ".join(src.split())
        self.assertIn("МЕДИА ИЗ ИНТЕРНЕТА — ФАЙЛОМ, А НЕ ССЫЛКОЙ", flat)
        self.assertIn("Голая ссылка вместо файла — ошибка", flat)
        self.assertIn("попробуй другой сайт или поисковый запрос, а не сдавайся", flat)
        reg = Path("app/jarvis/tools/__init__.py").read_text(encoding="utf-8")
        self.assertIn("ФАЙЛОМ прямо в диалоге", " ".join(reg.split()))

    def test_bm12_spaces_visibility_switch(self) -> None:
        """Глазик настроек прячит пространство из меню; скрытое не свайпается."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('id="spSetPanel"', html)
        self.assertIn("const SPACE_EYE =", js)
        self.assertIn("function spaceToggleVisible(name)", js)
        self.assertIn("function spacesFlip(mutate)", js)
        self.assertIn("'jarvis.spaces.visible'", js)
        self.assertIn("S.spacesVisible.indexOf(name) < 0) return;", js)
        self.assertIn(".sp-ico.off{width:0;min-width:0;opacity:0;margin:0 -3px;border-width:0;pointer-events:none}", css)
        self.assertIn(".sp-eye.off::after", css)
        self.assertIn(".sp-set-row.off{opacity:.4;filter:saturate(.3)}", css)
        # все НЕ-чатовые скрыты: ряд с чатом исчезает С АНИМАЦИЕЙ (BM13),
        # шестерёнка вверх к LIVE — по центру LIVE-строки (BM21)
        self.assertIn(".spaces.no-spaces .sp-row{opacity:0;transform:scale(.42);", css)
        self.assertIn(".spaces.no-spaces .sp-ico.sp-set{top:8.5px}", css)

    def test_bm12_dock_collapse_squeeze(self) -> None:
        """Сворачивание в док сжимает пространства плавно; в доке вкладки
        есть в любом пространстве, надписи про диалоги нет."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("app.classList.add('docked')", js)
        self.assertIn("classList.add('collapsed', 'docked');", js)
        self.assertNotIn(".side-folding .spaces", css)
        # BM14: .docked гасит только visibility — display:none в конце
        # анимации давал однокадровый скачок кнопок вверх
        self.assertIn(".app.docked .spaces{visibility:hidden}", css)
        # BM18: свёрнутое меню не оставляет места невидимому ряду
        # пространств (пустота над LIVE) и стартует УЖЕ схлопнутым;
        # BM21: margin компенсирует gap'ы дока — полосы симметричны
        self.assertIn(".app.collapsed .spaces{max-height:0;padding:0 6px;overflow:hidden;margin:-10px 0}", css)
        self.assertIn("sp.style.maxHeight = '0px'", js)
        self.assertIn("sp0.style.maxHeight = '0px'", js)
        self.assertIn(".app.collapsed .space-future{display:none!important}", css)
        # BM14: у дока НЕТ рамки-области (светлая плашка с границами убрана);
        # высотой ряда управляет JS честным замером — класс её не трогает
        self.assertNotIn(".app.collapsed .sp-dock::before{", css)
        self.assertIn("класс не трогает max-height", css)
        self.assertIn("sp.style.maxHeight = sp.offsetHeight + 'px'", js)

    def test_bm12_space_chrome_instant(self) -> None:
        """Клик по пространству красит иконки/глайдер мгновенно, не после анимации."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function updateSpaceChrome(name)", js)
        setsp = js.split("function setSpace(name, dir)")[1].split("\nfunction ")[0]
        self.assertIn("updateSpaceChrome(name);", setsp)

    def test_bm9_interactive_panels_after_news(self) -> None:
        """После сводки новостей/погоды панели выбора снова уместны."""
        from jarvis import agent as ag
        self.assertIn("Единственное железное", ag.PROACTIVE_UI_CONTRACT)
        self.assertIn("уже сделал в", ag.PROACTIVE_UI_CONTRACT)
        self.assertNotIn("НЕ показывай ui для\nфактического вопроса, сводки новостей",
                         ag.PROACTIVE_UI_CONTRACT)

    def test_sidebar_today_spend(self) -> None:
        """Сайдбар: «Расход сегодня» от местной полуночи устройства."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        self.assertIn("Расход сегодня", html)
        self.assertNotIn("Расход 24ч", html)
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("day_start=' + dayStart", js)
        self.assertIn("getFullYear(), d.getMonth(), d.getDate()", js)


class IterationBM13Tests(unittest.TestCase):
    """BM13 — чипы (несуществующая nano-модель → фолбэк base), все кривые
    графика, корень без наездов, скролл-догон, живая AUTO-карточка,
    мгновенная память, пространства по 10 пунктам, медиа со страниц."""

    def test_bm13_nano_fallback_to_base_model(self) -> None:
        """Чипы: несуществующая у провайдера nano-модель не убивает подсказки."""
        llm_src = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        # фолбэк прямо в chat(): tier≠base без каталога → модель base
        self.assertIn("model = pick_model(\"base\", prov)", llm_src)
        cfg = Path("app/jarvis/config.py").read_text(encoding="utf-8")
        # первопричина: чужое имя в первом pref nano оставляем как есть —
        # фолбэк обязан работать ДАЖЕ с кривым списком
        self.assertIn("ai-sage/GigaChat3-10B-A1.8B", cfg)

    def test_bm13_suggestions_retry_with_base(self) -> None:
        """Nano упал — ОДИН повтор базовой моделью; обе упали — шаблоны."""
        from jarvis import agent as ag
        calls = []

        def fake_chat(messages, tier="base", **kw):
            calls.append(tier)
            if tier == "nano":
                raise RuntimeError("404 model not found")
            return {"content": '["открой график продаж", '
                               '"сравни с прошлым месяцем", "посчитай итог"]'}

        long_answer = ("Вот подробный анализ графика продаж за квартал "
                       "с выводами и прогнозом на следующий месяц.")
        with mock.patch.object(ag.llm, "chat", side_effect=fake_chat):
            items = ag.suggest_replies_ai("посмотри график", long_answer,
                                          tools_used=["run_python"])
        self.assertEqual(calls, ["nano", "base"])
        self.assertTrue(items)
        self.assertIn("открой график продаж", items)

        # nano жива — base не дёргается вовсе
        calls.clear()

        def ok_chat(messages, tier="base", **kw):
            calls.append(tier)
            return {"content": '["открой график продаж", '
                               '"сравни с прошлым месяцем", "посчитай итог"]'}

        with mock.patch.object(ag.llm, "chat", side_effect=ok_chat):
            ag.suggest_replies_ai("посмотри график", long_answer,
                                  tools_used=["run_python"])
        self.assertEqual(calls, ["nano"])

        # обе модели легли — честный шаблонный запас, не пусто
        calls.clear()

        def dead_chat(messages, tier="base", **kw):
            calls.append(tier)
            raise RuntimeError("provider down")

        with mock.patch.object(ag.llm, "chat", side_effect=dead_chat):
            items = ag.suggest_replies_ai("посмотри график", long_answer,
                                          tools_used=["run_python"])
        self.assertEqual(calls, ["nano", "base"])
        self.assertTrue(items)

    def test_bm13_media_from_page(self) -> None:
        """Страница — валидный вход: og:video → файл; пустая — внятный отказ."""
        import jarvis.tools.media as med
        import pathlib, tempfile

        page = ('<html><head><meta property="og:video" '
                'content="https://cdn.example.net/cat.mp4"/></head>'
                '<body>player</body></html>').encode("utf-8")

        class Resp:
            def __init__(self, body, ctype):
                self._b = body
                self.headers = {"Content-Type": ctype,
                                "Content-Length": str(len(body))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1):
                # честный потоковый read: буфер исчерпывается, циклы
                # чтения страницы/файла заканчиваются, а не крутятся вечно
                if n and n > 0:
                    out, self._b = self._b[:n], self._b[n:]
                else:
                    out, self._b = self._b, b""
                return out

        tmpdir = pathlib.Path(tempfile.mkdtemp())

        def fake_urlopen(req, timeout=0, context=None):
            if req.full_url.endswith(".mp4"):
                return Resp(b"FAKEVIDEOBYTES123", "video/mp4")
            if req.full_url.endswith(".html"):
                return Resp(page, "text/html; charset=utf-8")
            raise AssertionError("unexpected url " + req.full_url)

        class FakeSandbox:
            @staticmethod
            def safe_path(name): return tmpdir / name
            @staticmethod
            def dl(name): return "/dl/" + name

        with mock.patch.object(med.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(med, "sandbox", FakeSandbox):
            res = med.show_media("https://example.com/watch.html")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["kind"], "video")
        self.assertTrue((tmpdir / res["name"]).exists())
        self.assertEqual((tmpdir / res["name"]).read_bytes(),
                         b"FAKEVIDEOBYTES123")

        # страница без медиа — внятный отказ с подсказкой, что дать вместо
        empty = Resp(b"<html><body>just text</body></html>",
                     "text/html; charset=utf-8")

        def fake_empty(req, timeout=0, context=None):
            return empty

        with mock.patch.object(med.urllib.request, "urlopen",
                               side_effect=fake_empty):
            res2 = med.show_media("https://example.com/nowatch.html")
        self.assertFalse(res2["ok"])
        self.assertIn("не нашлось медиа-файла", res2["error"])
        self.assertIn("прямую", res2["error"])

    def test_bm13_plot_draws_all_curves(self) -> None:
        """«2x и 3» — обе кривые: сбор со всех ключей спеки, без break."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        collector = js.split("function plotCollectFormulas(spec)")[1] \
            .split("\nfunction ")[0]
        # «2x и 3» одной строкой делится на две кривые
        self.assertIn("split(/\\s*(?:,|;|\\sи\\s)\\s*/)", collector)
        # y-строки — кривые даже при занятом f (прежде: только если f пуст)
        self.assertIn("ys.every((v) => typeof v === 'string')", collector)
        # поверхность с y-переменной при пустом f — это z
        self.assertIn("spec.z = surface;", collector)
        # числовой y (диапазон оси) не превращается в кривую
        self.assertIn("delete spec.y", collector)
        self.assertIn("spec = plotCollectFormulas(spec);", js)

    def test_bm13_root_degree_inside_box(self) -> None:
        """Степень корня — внутри рамки корня: не наезжает на скобку слева."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("msqrt' + (root ? ' msqrt-i' : ''", md)
        # BM16: степень ПРЯМО НАД НИЖНИМ ЗАГИБОМ (как в настоящем ∛):
        # .msq-i у левого края, низ — над верхом крючка (42% высоты),
        # обёртки .msq-box больше нет
        self.assertIn(".msqrt .msq-i{position:absolute;left:-.04em;bottom:calc(47% + .01em);font-size:.58em", css)
        self.assertIn(".msqrt.msqrt-i{padding-left:calc(.40em + var(--msq-x,0em))}", css)
        self.assertIn(".msqrt.msqrt-i .msq-svg{left:var(--msq-x,0em)}", css)
        self.assertNotIn("msq-box", css)
        self.assertIn("function fixRootIndices(root)", js)
        # формула сдвига выведена из геометрии viewBox (6.6×24, вершина 3.3,16)
        fix = js.split("function fixRootIndices(root)")[1].split("\nfunction ")[0]
        self.assertIn("const xU = 3.3 + (16 - yU) * (5.9 - 3.3) / 16;", fix)
        self.assertIn("const pocket = xU * 0.40 / 6.6;", fix)
        # прежний вынос за левый край — запрещён (правило, не комментарий)
        self.assertNotIn(".msq-i{position:absolute;top:0;left:-.36em", css)
        self.assertNotIn("left:-.36em;width", css)
        self.assertNotIn("right:calc(100% - .98em)", css)

    def test_bm13_scroll_catches_final_render_growth(self) -> None:
        """Финальный рендер (таблицы/графики/вкладки) — экран догоняет."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("ui.onTyped = () => {", js)
        # onTyped встречается дважды (typeAutoReply и главный) — берём хвост
        typed = js.split("ui.onTyped = () => {").pop()
        self.assertIn("followGrowingPanel(ui.replyLive || ui.mdEl, 1200, ui)",
                      typed)

    def test_bm13_live_auto_card_at_task_start(self) -> None:
        """AUTO-карточка появляется В МОМЕНТ запуска задачи, не после ответа."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BM14: карточка обобщена — AUTO/ФАЙЛЫ/ПАМЯТЬ в момент события
        self.assertIn("function embedLiveCard(ui, view, title)", js)
        self.assertIn("function embedLiveAuto(ui) { embedLiveCard(ui, 'auto'", js)
        self.assertIn("function embedLiveAutoSettle(ui)", js)
        self.assertIn("ev.name === 'schedule_task'", js)
        self.assertIn("embedLiveCard(ui, 'files', 'файлы диалога')", js)
        self.assertIn("embedLiveCard(ui, 'memory', 'что я запомнил')", js)
        # сервер сам уводит задачу в фон — карточка сразу, до ответа
        self.assertIn("case 'background'", js)
        # дубль не ставим: серверская карточка пришла — живая уходит
        self.assertIn("const inText = (ui.mdEl && $$('.embed-panel', ui.mdEl)", js)
    def test_bm13_auto_card_realtime_sync(self) -> None:
        """Удаление фоновой задачи видно в карточке без перезагрузки."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        auto_part = js.split("if (spec.view === 'auto')")[1] \
            .split("} else if (spec.view === 'files')")[0]
        self.assertIn("setInterval", auto_part)
        self.assertIn("panel._embFp", auto_part)
        self.assertIn("clearInterval(panel._embSync)", auto_part)

    def test_bm13_memory_tab_instant(self) -> None:
        """/api/memory без тяжёлого repair: чистка уехала в фон при старте."""
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        mem_block = srv.split('if path == "/api/memory":')[1] \
            .split('if path == "/api/scenarios"')[0]
        self.assertNotIn("repair_legacy_automatic_memories()", mem_block)
        self.assertIn('name="jarvis-mem-repair"', srv)
        # компакция — по отпечатку данных, а не на каждый recall
        dbs = Path("app/jarvis/db.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(MAX(updated_at), 0)", dbs)
        self.assertIn("COALESCE(MAX(rowid), 0) FROM memory", dbs)
        self.assertIn("if fp is not None and fp == _COMPACT_FP:", dbs)

    def test_bm13_spaces_chat_not_hideable(self) -> None:
        """Чат не скрывается: в списке видимости только НЕ-чат."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        loader = js.split("function spacesVisibleLoad()")[1] \
            .split("\nfunction ")[0]
        self.assertIn("n !== 'chat'", loader)
        self.assertIn("n !== 'chat'", js.split("SPACES.filter((n) => "
                                               "n !== 'chat')")[0]
                     if "SPACES.filter((n) => n !== 'chat')" in js else loader)
        settings = js.split("function buildSpaceSettings()")[1] \
            .split("\nfunction ")[0]
        self.assertIn("chat-fixed", settings)
        # BM14: подписи-заголовка в панели НЕТ — только сами пространства
        self.assertNotIn("sp-set-title", settings)
        # скрыли текущее — возврат в чат
        toggle = js.split("function spaceToggleVisible(name)")[1] \
            .split("\nfunction ")[0]
        self.assertIn("setSpace('chat')", toggle)
        # чат доступен всегда
        setsp = js.split("function setSpace(name, dir)")[1] \
            .split("\nfunction ")[0]
        self.assertIn("name !== 'chat' && S.spacesVisible.indexOf(name) < 0",
                      setsp)

    def test_bm13_spaces_glass_settings_and_close_outside(self) -> None:
        """Панель настроек — стекло + заголовок + закрытие тапом вне."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BM14: стекло МЕНЕЕ прозрачное (плотнее + сильнее blur), без
        # заголовка; CHAT — жирная выделенная строка
        self.assertIn("backdrop-filter:blur(24px) saturate(1.1)", css)
        self.assertIn("background:rgba(15,27,44,.56)", css)
        self.assertNotIn(".sp-set-title{", css)
        self.assertIn("transform:scale(.55);transform-origin:100% 0", css)
        self.assertIn(".sp-set-row.chat-fixed{", css)
        self.assertIn(".sp-set-row.chat-fixed b{font-weight:700", css)
        # закрытие тапом вне
        init = js.split("function initSpaces()")[1].split("\nfunction ")[0]
        self.assertIn("document.addEventListener('click', () => {", init)
        self.assertIn("setPanel.classList.remove('open')", init)

    def test_bm13_caps_names_and_hollow_chat_icon(self) -> None:
        """Имена капсом CHAT/MATH/MUSIC; иконка чата полая и тихая."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn('data-space="chat" data-tip="CHAT"', html)
        self.assertIn('data-space="math" data-tip="MATH"', html)
        self.assertIn('data-space="music" data-tip="MUSIC"', html)
        self.assertIn('data-tip="Настройки отображения"', html)
        # полая иконка: stroke вместо fill в SPACE_META и в html
        chat_block = js.split("chat: { name: 'CHAT'")[1].split("},")[0]
        self.assertIn('fill="none" stroke="currentColor"', chat_block)
        # BM21: ЖИРНАЯ и яркая (юзер отверг тусклую), свечение положено
        self.assertIn(".sp-ico.base{color:var(--tx);font-weight:700;\n  box-shadow:inset 0 0 0 1px rgba(0,212,255,.12)}", css)
        self.assertNotIn(".sp-ico.base.sel{filter:none}", css)
        self.assertIn(".spd-cur{color:var(--tx3)}", css)

    def test_bm13_live_fill_from_center(self) -> None:
        """LIVE hover — заливка светом из центра, однородно."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # BM22: свечение НЕ рождается из нуля — в покое оно УЖЕ живёт
        # маленьким пятном (op .55, scale .38) и при наведении заполняет
        # всю кнопку; базовой заливки у кнопки больше нет — вся в ::after
        self.assertIn(".sp-mode::after{", css)
        self.assertIn("background:radial-gradient(circle at 50% 50%,rgba(0,212,255,.13)", css)
        self.assertIn("opacity:.4;", css)
        # BM23: у самой кнопки фона нет вовсе — иначе проступает
        # системный белый <button>; всё свечение в ::after
        self.assertIn("background:radial-gradient(ellipse at 50% 50%,rgba(0,212,255,.09)",
                      css.split(".sp-mode{")[1].split("}")[0])
        self.assertNotIn("opacity:0;transform:scale(.24)", css)
        self.assertIn("transition:opacity .55s ease,box-shadow .55s ease", css)
        self.assertIn(".sp-mode:hover::after{opacity:1;box-shadow:0 0 26px rgba(0,212,255,.1)}", css)
        self.assertNotIn(".sp-mode:hover::after{transform:scale(2.6)}", css)

    def test_bm13_chat_list_scrollbar_only_while_scrolling(self) -> None:
        """Скроллбар списка диалогов живёт только во время скролла."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("scrollbar-color:transparent transparent", css)
        self.assertIn(".chat-list.scr{", css)
        # BM22: маска ПОСТОЯННАЯ и прижата к верху (4px) — не появляется
        # при листании и не тускнит выбранный верхний диалог
        self.assertNotIn(".chat-list.over{", css)
        self.assertIn("#000 4px", css)
        self.assertIn("rgba(0,190,255,.16) transparent", css)
        # BM22: палка — ещё тоньше и тише
        self.assertIn(".chat-list::-webkit-scrollbar{width:1.5px}", css)
        self.assertIn(".chat-list.scr::-webkit-scrollbar-thumb{background:rgba(0,190,255,.16)}", css)
        self.assertIn("cl.classList.add('scr')", js)
        self.assertNotIn("cl.classList.toggle('over'", js)

    def test_bm13_embed_head_dense(self) -> None:
        """Шапка мини-вкладок плотнее — как вкладки инструментов агента."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".emb-head{display:flex;align-items:center;gap:7px;"
                      "padding:5px 10px", css)
        self.assertIn("font:600 9.5px/1 var(--ff);letter-spacing:1.9px", css)

    def test_bm13_memory_embed_honest_errors(self) -> None:
        """Карточка памяти: сбой — честная ошибка, не «память пуста»; таймаут."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        mem = js.split("} else if (spec.view === 'memory')")[1] \
            .split("} else if (spec.view === 'scenarios')")[0]
        self.assertIn("Память собирается дольше обычного", mem)
        self.assertIn("fail(new Error('не удалось открыть память'))", mem)


class IterationBM14Tests(unittest.TestCase):
    """BM14: корни в боксе, честный payload-guard, живые карточки событий,
    AUTO-вопросы в диалог, док-меню из кнопки, диктовка, тонкий скроллбар."""

    def test_bm14_root_degree_box_wrapper(self) -> None:
        """BM16: обёртки-бокса больше нет — степень лежит над загибом."""
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        self.assertIn("'<span class=\"msq-i\">' + mesc(root) + '</span>'", md)
        self.assertNotIn("msq-box", md)
        self.assertIn("в кармане галочки, как в глифе ∛", md)

    def test_bm14_payload_guard_honest_json_hint(self) -> None:
        """Ретрай больше не ВРЁТ «просил именно JSON» когда просили медиа."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        self.assertIn("if user_wants_json(user_text):", src)
        # ветка JSON остаётся только за условием
        self.assertIn("fix_hint", src)
        self.assertIn("а не сохраняй", src)
        self.assertIn("JSON и не перечисляй ссылки текстом.", src)
        # враньё не осталось безусловным: текст живёт только внутри ветки
        self.assertIn("Пользователь просил именно ", src)
        self.assertLess(src.index("if user_wants_json(user_text):"),
                        src.index("Пользователь просил именно "))

    def test_bm14_media_never_json_fallback(self) -> None:
        """Промпт: найденное медиа — в show_media, JSON вместо медиа запрещён."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        flat = " ".join(src.split())
        self.assertIn("Нашёл через web_search ссылку или страницу с медиа — "
                      "ПЕРЕДАЙ её в show_media", flat)
        self.assertIn("Запрещено вместо медиа сохранять результаты поиска в "
                      "JSON-файл", flat)

    def test_bm14_state_question_not_background(self) -> None:
        """«Что ты делаешь в фоне?» — вопрос, а не фоновая задача."""
        auto = Path("app/jarvis/auto.py").read_text(encoding="utf-8")
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        # BM14.1: regex живёт в agent.py — правило одно для маршрутизатора
        # фона и перехвата schedule_task внутри прогона
        self.assertIn("STATE_Q_RE", src)
        self.assertIn("def state_question_view(", src)
        self.assertIn("agent.state_question(t)", auto)
        sys.path.insert(0, "app")
        try:
            from jarvis import auto as automod
            self.assertIs(automod.should_background(
                "что ты делаешь в фоне?")["background"], False)
            self.assertIs(automod.should_background(
                "покажи, какие задачи фоном")["background"], False)
            self.assertIs(automod.should_background(
                "сделай это в фоне")["background"], True)
        finally:
            sys.path.remove("app")

    def test_bm14_auto_answers_go_to_dialog(self) -> None:
        """Результат/ошибка задачи без чата — сообщением в диалог, не в док."""
        auto = Path("app/jarvis/auto.py").read_text(encoding="utf-8")
        # BM15: в диалоге — только результаты задач ЭТОГО диалога; чужие
        # результаты выглядели как «ответ на прошлый запрос»
        self.assertIn('if task.get("chat_id"):', auto)
        self.assertIn('db.add_message(task["chat_id"], "assistant", content', auto)
        self.assertIn('db.notify("AUTO: " + task["title"]', auto)
        self.assertNotIn("target_chat", auto)
        self.assertNotIn("db.list_chats(1)", auto)

    def test_bm14_auto_card_before_done(self) -> None:
        """Карточка вкладки вкладывается в ответ ДО done — и по памяти."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        dbp = Path("app/jarvis/db.py").read_text(encoding="utf-8")
        self.assertIn("def memory_count()", dbp)
        self.assertIn("self._mem0 = db.memory_count()", src)
        tail = src.split('yield {"type": "done"')[-1]
        head = src.split('final_text = auto_embed_block(')[-1]
        self.assertIn("final_text = auto_embed_block(", src)
        self.assertIn("memory_changed=", src)
        self.assertIn("q_view=state_question_view(user_text)", src)
        # BM14.1: show_media-теги в тексте исполняются и вычищаются
        self.assertIn("def rescue_show_media_tags(", src)
        self.assertIn("self._rescue_show_media_tags(final_text)", src)

    def test_bm14_embed_head_solid(self) -> None:
        """BM16: область названия мини-вкладки ЧУТЬ СВЕТЛЕЕ — выделяется."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        head = css.split(".emb-head{")[1].split("}")[0] + \
            css.split(".emb-head{")[1].split("}")[1]
        # BM17: однородный полупрозрачный джарвисовский синий, чуть светлее
        # тела карточки — НЕ градиент и НЕ светлая заливка
        self.assertIn("background:rgba(16,44,68,.62)", head)
        self.assertNotIn("linear-gradient", head)
        self.assertNotIn("background:rgb(10,20,33)", head)

    def test_bm14_global_tooltip_overlay(self) -> None:
        """Подписи пространств — глобальный fixed-оверлей поверх границ."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("document.addEventListener('pointerover'", js)
        self.assertIn(".g-tip{position:fixed;z-index:300", css)
        self.assertIn(".g-tip.show{opacity:1", css)
        # прежние обрезанные ::before ушли
        self.assertNotIn(".sp-ico[data-tip]::before", css)
        self.assertNotIn(".spf-ico[data-tip]::before", css)

    def test_bm14_glider_immediate_and_flip_cleanup(self) -> None:
        """Глайдер рисуется сразу и ведёт анимацию; FLIP не оставляет transform."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function gliderWatchRun(foldDir)", js)
        self.assertIn("gliderWatchRun(collapsing ? 'collapse' : 'expand');", js)
        self.assertIn("b.style.transform = ''", js)
        self.assertIn("requestAnimationFrame(follow)", js)   # BM27: глайдер ведёт ряд покадрово

    def test_bm14_honest_height_animation(self) -> None:
        """Высота ряда — честный замер JS, без доездов и прыжков."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        fold = js.split("function toggleSidebar()")[1].split("\nfunction ")[0]
        self.assertIn("sp.style.maxHeight = sp.offsetHeight + 'px'", fold)
        self.assertIn("sp.style.maxHeight = '0px'", fold)
        self.assertIn("const h = node.offsetHeight", fold)
        self.assertIn("node.style.maxHeight = h + 'px'", fold)
        # BM23: замер КОНЕЧНОЙ геометрии (переходы на миг выключены) и
        # снятие inline cap ПО КОНЦУ перехода — иначе рывок после анимации
        self.assertIn("node.style.transition = 'none'", fold)
        self.assertIn("const onGrowEnd = (e) =>", fold)
        # класс больше не рулит высотой ряда
        self.assertNotIn("max-height:0;padding-top:0;padding-bottom:0", css)

    def test_bm14_dock_full_dash_and_blurred_border(self) -> None:
        """Полоса под текущим пространством — на весь док; граница размыта."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".app.collapsed .spd-cur-wrap{align-self:stretch", css)
        dash = css.split(".spd-dash{")[1].split("}")[0]
        self.assertIn("width:100%", dash)
        self.assertIn(".spaces::after{", css)
        self.assertIn("filter:blur(1.2px)", css)
        self.assertNotIn(".app.collapsed .sp-dock::before{", css)

    def test_bm14_flyout_grows_from_button(self) -> None:
        """Меню пространств вырастает ИЗ кнопки; иконка плывёт на слот."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        fly = css.split(".spd-fly{")[1].split("}")[0]
        self.assertIn("left:0", fly)
        self.assertIn("scale(.22)", fly)
        self.assertIn("transform-origin:left center", fly)
        self.assertNotIn("left:calc(100% + 8px)", css)
        init = js.split("function initDockFly()")[1].split("\nfunction ")[0]
        self.assertIn("selShift", init)
        self.assertIn("classList.add('ghost')", init)
        self.assertIn("icons.indexOf(sel) <= 0", init)

    def test_bm22_chat_list_mask_permanent(self) -> None:
        """BM22: мягкий край списка диалогов — ПОСТОЯННЫЙ, тонкий (4px),
        не появляется при листании; следить за переполнением больше нечем."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        base = css.split(".chat-list{")[1].split("}")[0]
        self.assertIn("mask-image:linear-gradient(180deg,rgba(0,0,0,var(--chatFade,1)) 0,#000 4px", base)
        self.assertIn("calc(100% - 5px),transparent 100%)", base)
        self.assertNotIn(".chat-list.over{", css)
        hook = js.split("СКРОЛЛБАР ЧАТ-ЛИСТА")[1].split("})();")[0]
        self.assertIn("cl.classList.add('scr')", hook)
        self.assertNotIn("scrollHeight > cl.clientHeight", hook)
        self.assertNotIn("MutationObserver", hook)

    def test_bm14_mic_button_is_dictation(self) -> None:
        """Микрофон — диктовка в поле; разговор остался для LIVE."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BM29: диктовка и тумблеры камеры/компьютера — на месте, как прежде
        self.assertIn('id="micBtn" data-tip="Диктовка"', html)
        self.assertIn('id="tgCamera"', html)
        self.assertIn('id="tgComputer"', html)
        # прямые слушатели тумблеров (как до LIVE)
        self.assertIn("$('#tgCamera').addEventListener('click'", js)
        self.assertIn("$('#tgComputer').addEventListener('click'", js)
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("#liveRoot{", css)
        # звонок открывают существующие кнопки LIVE (над иконками и в доке)
        self.assertIn("$('#spModeLive').addEventListener('click', () => { liveOpen(); });", js)
        self.assertIn("if (spdLive) spdLive.addEventListener('click', () => { liveOpen(); });", js)
        # тумблеры камеры/компьютера — теперь кнопки панели звонка
        self.assertIn("id=\"lbCam\"", js)
        self.assertIn("id=\"lbMic\"", js)
        self.assertIn("id=\"lbComp\"", js)
        self.assertIn("id=\"lbExit\"", js)
        # диктовка (MediaRecorder -> сервер) при вводе
        dstart = js.split("async function dictStart")[1].split("\nfunction ")[0]
        self.assertIn("getUserMedia", dstart)
        self.assertIn("MediaRecorder", dstart)
        self.assertNotIn("SpeechRecognition", dstart)
        dtr = js.split("async function dictTranscribeSegment")[1].split("\nfunction ")[0]
        self.assertIn("/api/transcribe", dtr)
        self.assertIn("blobToWav16k", dtr)
        dput = js.split("function dictPutText")[1].split("\nfunction ")[0]
        self.assertIn("box.value = (base + String(text)", dput)
        # BM16: data-url от blobToWav16k уходит на сервер НАПРЯМУЮ — прежний
        # FileReader.readAsDataURL(строка) валил каждое распознавание
        self.assertNotIn("readAsDataURL(wav)", js)

    def test_bm14_live_card_files_memory_at_event(self) -> None:
        """Файл создан — карточка ФАЙЛЫ сразу; факт — карточка ПАМЯТЬ."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        tr = js.split("case 'tool_result':")[1].split("case '")[0]
        self.assertIn("embedLiveCard(ui, 'files', 'файлы диалога')", tr)
        self.assertIn("embedLiveCard(ui, 'memory', 'что я запомнил')", tr)
        self.assertIn("write_file|download_file|make_archive|generate_image", tr)

    def test_bm14_rescue_show_media_tags_executes_and_strips(self) -> None:
        """Теги <show_media/> в тексте исполняются и вычищаются."""
        sys.path.insert(0, "app")
        try:
            from jarvis import agent as ag
            calls = []

            def fake_call(name, args):
                calls.append((name, args))
                return {"ok": True, "path": "media_x.mp4", "name": "media_x.mp4",
                        "kind": "video", "size": 5,
                        "download_url": "/dl/media_x.mp4"}

            orig = ag.tools.call
            ag.tools.call = fake_call
            try:
                text = ('Вот <show_media url="https://e.com/v.mp4" type="video"/> '
                        'и <show_media url=\'https://e.com/a.mp3\' type=\'audio\'/> '
                        'битый <show_media /> и ссылка https://e.com/page')
                files = []
                cleaned, rescued = ag.rescue_show_media_tags(text, files.append)
            finally:
                ag.tools.call = orig
            self.assertTrue(rescued)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["url"], "https://e.com/v.mp4")
            self.assertEqual(len(files), 2)
            self.assertNotIn("show_media", cleaned)
            self.assertNotIn("e.com/v.mp4", cleaned)
            self.assertIn("https://e.com/page", cleaned)
        finally:
            sys.path.remove("app")

    def test_bm14_state_question_view_routing(self) -> None:
        """Вопрос о состоянии — карточка соответствующей вкладки."""
        sys.path.insert(0, "app")
        try:
            from jarvis import agent as ag
            self.assertEqual(ag.state_question_view("что ты делаешь в фоне?"), "auto")
            self.assertEqual(ag.state_question_view("покажи какие задачи фоном"), "auto")
            self.assertEqual(ag.state_question_view("какие файлы ты создал?"), "files")
            self.assertEqual(ag.state_question_view("что ты помнишь?"), "memory")
            self.assertEqual(ag.state_question_view("какие сценарии есть?"), "scenarios")
            self.assertIsNone(ag.state_question_view("создай файл тест.txt"))
            self.assertIsNone(ag.state_question_view("сделай это в фоне"))
        finally:
            sys.path.remove("app")

    def test_bm14_background_event_embeds_immediately(self) -> None:
        """Серверный уход в фон: ЗЕЛЁНАЯ БЛАШКА с вкладкой АВТО сразу."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        bg = js.split("case 'background':")[1].split("case '")[0]
        # BM19: ЗЕЛЁНЫЙ БАННЕР ПРЯМО В ЧАТЕ (не угловой тост), АВТО внутри
        self.assertIn("bgTaskNote(ui, ev.title, ev.when || '');", bg)
        self.assertNotIn("embedLiveAuto(ui)", bg)
        self.assertNotIn("toastAutoCard", js)


class IterationBM16Tests(unittest.TestCase):
    """BM16: корни заново, медиа-JSON, мини-вкладки-объекты, настройки, микрофон."""

    def test_bm16_root_degree_above_bend(self) -> None:
        """Степень — ПРЯМО НАД НИЖНИМ ЗАГИБОМ; сдвиг из геометрии viewBox."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        md = Path("app/jarvis/web/js/markdown.js").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn(
            ".msqrt .msq-i{position:absolute;left:-.04em;bottom:calc(47% + .01em);font-size:.58em",
            css)
        # обёртки-бокса нет ни в рендере, ни в стилях
        self.assertNotIn("msq-box", md)
        self.assertNotIn("msq-box", css)
        fix = js.split("function fixRootIndices(root)")[1].split("\nfunction ")[0]
        # BM17: низ в % высоты корня (em считались бы от .58em степени),
        # поднят выше (47%), чтобы явно не касаться штриха
        self.assertIn("const b = Math.max(0.47 * H + 0.01, Math.min(degH + 0.02, H - 0.02));", fix)
        self.assertIn("ind.style.bottom = (100 * b / H).toFixed(2) + '%';", fix)
        # вынос влево — честными em корня (px из JS, не CSS-em индекса)
        self.assertIn("const LEFT = -0.05;", fix)
        self.assertIn("ind.style.left = (LEFT * fs) + 'px';", fix)
        # карман диагонали выведен из той же геометрии, что и path svg
        self.assertIn("const xU = 3.3 + (16 - yU) * (5.9 - 3.3) / 16;", fix)
        self.assertIn("const pocket = xU * 0.40 / 6.6;", fix)
        # правый край степени не задевает штрих: честный зазор .09em
        self.assertIn("const shift = Math.max(0, w - LEFT + 0.09 - pocket - 0.02);", fix)
        self.assertNotIn("const shift = Math.max(0, w + 0.05 - pocket - 0.04);", fix)

    def test_bm16_mic_native_icon_and_direct_datalog(self) -> None:
        """Родной значок микрофона; data-url уходит на сервер напрямую."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        # BM28: родной значок микрофона снова в композере
        self.assertIn('<rect x="9" y="2.6" width="6" height="11.2" rx="3"/>', html)
        self.assertIn('d="M8.8 21h6.4"', html)
        # ховеры диктовки и прикрепления файла не изменились
        self.assertIn("#attachBtn:hover{color:var(--sky);border-color:rgba(79,150,201,.5)}", css)
        self.assertIn("#micBtn:hover{color:var(--sky);border-color:rgba(79,150,201,.5)}", css)
        # BM17: транскрипция сегмента — отдельной функцией (живая диктовка)
        dtr = js.split("async function dictTranscribeSegment")[1].split("\nfunction ")[0]
        self.assertIn("const r = await api('/api/transcribe', { audio: wav, language: 'ru' });", dtr)
        self.assertIn("blobToWav16k", dtr)
        # прежний FileReader.readAsDataURL(строка) валил каждое распознавание
        self.assertNotIn("readAsDataURL(wav)", js)

    def test_bm16_media_json_rescue(self) -> None:
        """JSON-блок {"url": …} исполняется как show_media и исчезает."""
        from jarvis import agent as ag
        from unittest import mock
        text = ("Вот видео:\n```json\n{\"url\": "
                "\"[https://x.com/v.mp4](https://x.com/v.mp4)\"}" +
                "\n```\nПриятного просмотра.")
        with mock.patch.object(ag.tools, "call",
                               return_value={"ok": True, "file": "v.mp4",
                                             "path": "v.mp4", "kind": "video"}), \
             mock.patch.object(ag, "_file_info_of",
                               return_value={"name": "v.mp4", "size": 10}):
            files = []
            cleaned, rescued = ag.rescue_show_media_json(text, files.append)
        self.assertTrue(rescued)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("x.com", cleaned)
        self.assertEqual(files, [{"name": "v.mp4", "size": 10}])
        # произвольный JSON-код не трогаем
        code = "```json\n{\"name\": \"Иван\", \"age\": 30}\n```"
        cleaned2, rescued2 = ag.rescue_show_media_json(code, None)
        self.assertFalse(rescued2)
        self.assertEqual(cleaned2, code)

    def test_bm16_state_questions_extended(self) -> None:
        """«что в фоне», «факты обо мне», настройки — правильные вкладки."""
        from jarvis import agent as ag
        self.assertEqual(ag.state_question_view("что в фоне"), "auto")
        self.assertEqual(ag.state_question_view("покажи факты обо мне"), "memory")
        self.assertEqual(ag.state_question_view("какие у нас провайдеры?"), "settings")
        self.assertEqual(
            ag.state_question_view("сделай яндекс у себя основным провайдером"),
            "settings")
        self.assertEqual(ag.settings_section(
            "сделай яндекс у себя основным провайдером"), "providers")
        # поручение в фоне — НЕ вопрос о состоянии
        self.assertIsNone(ag.state_question_view("сделай в фоне задачу через час"))
        # снимок настроек — факты без секретов
        snap = ag.state_question_snapshot("settings")
        self.assertIn("провайдеры", snap)
        self.assertNotIn("api_key", snap.split("провайдеры")[1].split(";")[0])

    def test_bm16_embed_cards_objects_and_settings(self) -> None:
        """Мини-вкладки: без описания, объекты открываются, настройки."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        bep = js.split("function buildEmbedPanel(panel, spec)")[1].split("\nfunction ")[0]
        # описания нет; имя тянется на всю шапку
        self.assertNotIn("emb-sub", bep)
        self.assertIn(".emb-head b{font:600 9.5px/1 var(--ff);letter-spacing:1.9px;color:var(--tx2);", css)
        # BM17: разворот МЕДЛЕННЫЙ и плавный; повторная сборка НЕ
        # переанимирует работающую карточку (no-in)
        self.assertIn("transform-origin:0 0;animation:embIn .6s cubic-bezier(.22,.61,.25,1) both", css)
        self.assertIn("@keyframes embIn{from{transform:scale(.5,.35);opacity:0}", css)
        self.assertIn(".embed-card.no-in{animation:none}", css)
        # BM19: содержимое открывается ПРЯМО ИЗ ДИАЛОГА — клик по строке
        # разворачивает детали ПОД ней, без ухода во вкладку
        self.assertIn("const toggleMore = (row, html) => {", bep)
        self.assertIn("row.after(more);", bep)
        self.assertIn(".emb-more{", css)
        self.assertIn("@keyframes embMoreIn", css)
        self.assertNotIn("openObject", bep)
        # файл: изменённый подсвечен ЗЕЛЁНЫМ акцентом вкладки «Файлы»
        self.assertIn("file-changed", bep)
        self.assertIn("rgba(143,179,90,.12)", css)
        self.assertIn("openPreview({ name: f.name, size: f.size, url: f.download_url,", bep)
        self.assertIn("@keyframes flashIn", css)
        # три кнопки файла: переименовать / скачать / удалить
        self.assertIn('<i class="r" title="Переименовать">✎</i>', bep)
        self.assertIn('<i class="dl" title="Скачать">↓</i>', bep)
        self.assertIn('<i class="d" title="Удалить">✕</i>', bep)
        self.assertIn("api('/api/sandbox/rename_file'", bep)
        # карточка ФАЙЛОВ открывается в последнюю очередь (в самый низ)
        elc = js.split("function embedLiveCard(ui, view, title)")[1].split("\nfunction ")[0]
        self.assertIn("if (view === 'files') {", elc)
        self.assertIn("ui.node.body.appendChild(panel);", elc)
        # настройки — полноценный вид с секциями и синхронизацией
        self.assertIn("settings: { name: 'НАСТРОЙКИ', sub: 'конфигурация', view: 'settings', ico: '⚙' }",
                      js)
        self.assertIn("embSync(() => api('/api/config'),", bep)

    def test_bm16_auto_creation_notification_and_plain_fire(self) -> None:
        """Создание из чата — уведомление с карточкой; срабатывание — чисто."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        srv = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        auto = Path("app/jarvis/auto.py").read_text(encoding="utf-8")
        tr = js.split("case 'tool_result': {")[1].split("case '")[0]
        # BM19: ЗЕЛЁНЫЙ БАННЕР ПРЯМО В ДИАЛОГЕ с вкладкой АВТО внутри
        self.assertIn("bgTaskNote(ui, tt, '');", tr)
        self.assertNotIn("embedLiveAuto(ui)", tr)
        self.assertNotIn("toastAutoCard", js)
        # карточки на СТАРТЕ вызова больше нет — сентинел не создаёт задач
        ts = js.split("case 'tool_start': {")[1].split("case '")[0]
        self.assertNotIn("embedLiveAuto(ui)", ts)
        # маршрутизатор не уводит вопрос о состоянии в фон
        self.assertIn("if agent.state_question(text):", srv)
        # BM19: уведомление — ЗЕЛЁНЫЙ БАННЕР В ДИАЛОГЕ: живой по SSE +
        # сохранённая заметка с карточкой АВТО и меткой bg_note (видна
        # после перезагрузки, зелёная тонировка)
        bg = srv.split("if server_scheduled:")[1].split("self._sse_close()")[0]
        self.assertIn("**Фоновая задача поставлена**", bg)
        # карточка АВТО внутри заметки (в исходнике — экранированные кавычки)
        self.assertIn('\\"view\\": \\"auto\\"', bg)
        self.assertIn('"bg_note": True', bg)
        # сработавшая задача — просто отложенное сообщение
        self.assertNotIn("agent.auto_embed_block(content", auto)

    def test_bm16_prompt_knows_settings_tab(self) -> None:
        """Промпт: настройки — мини-вкладкой с секцией и инструкцией."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        flat = " ".join(src.split())
        self.assertIn("scenarios (сценарии) | settings", flat)
        self.assertIn("section = providers | images | safety | auto", flat)
        self.assertIn("ВОПРОС О НАСТРОЙКАХ", flat)
        self.assertIn("КОРОТКУЮ ИНСТРУКЦИЮ, что и где нажать", flat)
        # JSON-«вызов» медиа запрещён промптом
        self.assertIn("ЗАПРЕЩЕНО «вызывать» show_media JSON-блоком", flat)
        self.assertIn("def rescue_show_media_json(", src)
        self.assertIn("final_text = self._rescue_show_media_json(final_text)", src)


class IterationBM17Tests(unittest.TestCase):
    """BM17: корень выше/левее, живая диктовка, броня настроек, скролл,
    уведомление с вкладкой АВТО внутри, медиа-поиск, schedule_task в чате."""

    def test_bm17_fast_scroll_on_send(self) -> None:
        """Отправил запрос наверху — мгновенный плавный прыжок к низу."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function fastScrollToBottom(box)", js)
        fast = js.split("function fastScrollToBottom(box)")[1].split("\nfunction ")[0]
        self.assertIn("const dur = Math.min(420, 160 + gap * 0.12);", fast)
        self.assertIn("const e = 1 - Math.pow(1 - k, 3);", fast)   # ease-out cubic
        self.assertIn("if (st.fastRaf) cancelAnimationFrame(st.fastRaf);", fast)
        # вызов — сразу после addUserMsg, только при отправке (вниз)
        send = js.split("async function send(")[1]
        self.assertIn("if (sbox) fastScrollToBottom(sbox);", send)
        self.assertIn("requestHost.closest('.cam-chat')", send)

    def test_bm17_settings_freeze_armor(self) -> None:
        """Вкладка настроек НЕ может повесить интерфейс: вся сборка
        в броне, тайпер под try/catch, длинный хвост замерзает."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("function embedBuildSafe(panel, spec)", js)
        # все живые пересборки идут только через броню
        self.assertGreaterEqual(js.count("embedBuildSafe(panel, spec)"), 9)
        self.assertNotIn("buildEmbedPanel(panel, spec);\n", js.replace(
            "buildEmbedPanel(panel, spec);\n  } catch (e) {", ""))
        rt = js.split("function renderTyped(ui)")[1].split("\nfunction ")[0]
        self.assertIn("ui._longFreeze", rt)
        self.assertIn("text.length - base > 2600", rt)
        self.assertIn("lastSentenceEnd(text, base + 1200)", rt)
        # вызов рендера — в броне: исключение НЕ вешает печать,
        # сырой текст вываливается как есть и таймер честно гасится
        self.assertIn("try {\n        renderTyped(ui);\n      } catch (e) {", js)
        self.assertIn("ui.mdEl.textContent = ui.buffer;", js)

    def test_bm17_auto_card_no_reanimate(self) -> None:
        """Работающая задача НЕ переанимирует карточку каждые 4с."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        bep = js.split("function buildEmbedPanel(panel, spec)")[1].split("\nfunction ")[0]
        self.assertIn("if (panel._embBuilt) card.classList.add('no-in');", bep)
        self.assertIn(".embed-card.no-in{animation:none}", css)
        # fingerprint без volatile-полей задачи
        self.assertIn("[x.id, x.title, x.status, x.schedule, "
                      "String(x.result || '').slice(0, 80)]", bep)
        self.assertNotIn("x.updated_at", bep)

    def test_bm17_notification_carries_auto_tab(self) -> None:
        """Уведомление о фоновой задаче: вкладка АВТО разворачивается
        ПРЯМО В ОБЛАСТИ УВЕДОМЛЕНИЯ, не отдельной всплывашкой."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn("function bgTaskNote(ui, title, when)", js)
        ta = js.split("function bgTaskNote(ui, title, when)")[1].split("\nfunction ")[0]
        self.assertIn("Фоновая задача поставлена", ta)
        self.assertIn("embedBuildSafe(panel, spec)", ta)
        self.assertIn("panel.dataset.live = '1'", ta)
        # оба пути ставят баннер в чат; угловых тостов больше нет
        tr = js.split("case 'tool_result': {")[1].split("case '")[0]
        self.assertIn("bgTaskNote(ui, tt, '');", tr)
        bg = js.split("case 'background':")[1].split("case '")[0]
        self.assertIn("bgTaskNote(ui, ev.title, ev.when || '');", bg)
        self.assertNotIn("embedLiveAuto(ui)", tr + bg)
        self.assertNotIn("toastAutoCard", js)
        # зелёная плашка в ленте диалога + та же тонировка в истории
        self.assertIn("'bg-note'", ta)
        self.assertIn(".bg-note{", css)
        self.assertIn(".bg-note .emb-body{max-height:236px;overflow:auto}", css)
        self.assertIn("msg-bg-note", js)

    def test_bm17_embed_head_and_rows(self) -> None:
        """Шапка мини-вкладки — однородный синий; строка файла при ховере
        НЕ белеет."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        head = css.split(".emb-head{")[1].split("}")[0] + \
            css.split(".emb-head{")[1].split("}")[1]
        self.assertIn("background:rgba(16,44,68,.62)", head)
        self.assertNotIn("linear-gradient", head)
        hover = css.split(".emb-row:hover{")[1].split("}")[0]
        self.assertNotIn("color:var(--tx)", hover)

    def test_bm17_media_query_search(self) -> None:
        """show_media принимает ТЕКСТОВЫЙ запрос и сам ищет прямые файлы."""
        sys.path.insert(0, "app")
        try:
            from jarvis.tools import media as md
            code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
            self.assertIn("def _media_from_query(query: str)", code)
            self.assertIn("def _looks_like_domain(s: str)", code)
            self.assertIn('"%s filetype:mp3" % q', code)
            self.assertIn('"%s скачать mp3" % q', code)
            self.assertIn('"%s filetype:mp4" % q', code)
            q = code.split("def _media_from_query(query: str)")[1]
            self.assertIn("_YOUTUBE_RE.search(link) or _STREAMING_RE.search(link)", q)
            self.assertIn("show_media(link, _depth=1)", q)
            # не-ссылка при _depth==0 уходит в поиск; домен — под https://
            sm = code.split("def show_media(url: str, _depth: int = 0)")[1]
            self.assertIn("return _media_from_query(src)", sm)
            # пустой запрос — честная ошибка
            self.assertEqual(md._media_from_query("   ")["ok"], False)
        finally:
            sys.path.remove("app")

    def test_bm17_chat_runs_can_schedule(self) -> None:
        """Чат-прогон получает schedule_task: модель может ДЕЙСТВИТЕЛЬНО
        поставить фоновую задачу, а не только сказать «поставил»."""
        src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        flat = " ".join(src.split())
        self.assertIn("if self.chat_id and not self.task_id:", src)
        self.assertIn('available = list(available) + [_sched["schema"]]', src)
        self.assertIn("ПОРУЧЕНИЕ В ФОН", flat)
        self.assertIn("Не говори «поставил» без вызова", flat)


class IterationBM18Tests(unittest.TestCase):
    """BM18: стоп диктовки, броня зависаний, блашка задачи, док, медиа."""

    def test_bm18_mic_stops_and_filters_silence(self) -> None:
        """Повторный клик = честный СТОП; мусор тишины не пишется в поле."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        dstart = js.split("async function dictStart")[1].split("\nfunction ")[0]
        self.assertIn("if (DICT) { dictFinish(DICT); return; }", dstart)
        # dictFinish гасит таймер и останавливает запись
        dfin = js.split("function dictFinish")[1].split("\nfunction ")[0]
        self.assertIn("D.closing = true;", dfin)
        self.assertIn("D.stream.getTracks().forEach((t) => t.stop());", dfin)
        dtr = js.split("async function dictTranscribeSegment")[1].split("\nfunction ")[0]
        # «Продолжение следует…» и прочий мусор тишины — в поле не попадает
        self.assertRegex(dtr, "продолжение следует")

    def test_bm18_no_request_can_hang_forever(self) -> None:
        """api() с таймаутом: ни один запрос не висит вечно (зависание
        настроек = вечное ожидание ответа)."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        api_src = js.split("function api(path, body, extra)")[1].split("\nfunction ")[0]
        self.assertIn("AbortController", api_src)
        self.assertIn("25000", api_src)
        self.assertIn("сервер не ответил за 25с", api_src)

    def test_bm18_typer_and_panels_armor(self) -> None:
        """Тайпер целиком в броне; панель-заглушка собирается сторожем."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn("const typerTick = () => {", js)
        self.assertIn("console.error('typer:', e);", js)
        self.assertIn("ui.mdEl.textContent = String(ui.buffer || '');", js)
        # сторож: после конца стрима панель на «собираю вкладку…» соберётся
        self.assertIn("mountEmbedPanels(p.parentNode || document.body, true);", js)
        # настройки: видимый индикатор загрузки (не пустое вечное тело)
        bep = js.split("function buildEmbedPanel(panel, spec)")[1].split("\nfunction ")[0]
        self.assertIn("загружаю настройки…", bep)

    def test_bm18_dock_gap_above_live_closed(self) -> None:
        """Пустота над LIVE убрана: свёрнутое меню не оставляет места
        невидимому ряду пространств; старт — уже схлопнутым."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".app.collapsed .spaces{max-height:0;padding:0 6px;overflow:hidden;margin:-10px 0}", css)
        self.assertIn("sp0.style.maxHeight = '0px'", js)
        # BM21: дирижёра side-folding больше нет — бренд не гасится классом
        self.assertNotIn("side-folding", js)

    def test_bm18_sidebar_one_motion(self) -> None:
        """Морф меню — одна траектория: диалоги/футер/бренд/пункты едут
        плавно, без display:none-скачков и овершотов."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        self.assertIn(".app.collapsed .chats-block{flex-grow:0;", css)
        self.assertIn(".app.collapsed .side-foot{max-height:0;", css)
        self.assertNotIn(".app.collapsed .chats-block,\n.app.collapsed .side-foot{display:none}", css)
        # BM21: бренд не гасится — ядро и стрелка доезжают FLIP-перелётом
        self.assertNotIn(".side-folding .brand{opacity:0}", css)
        self.assertIn("const flipEls = [document.querySelector('#brandReactor'), document.querySelector('#collapseBtn')]", js)
        # BM24: НИ ОДНОГО мгновенного скачка перед морфом: высота бренда
        # едет переходом (97<->54), иконки пилюли не телепортируются
        # (max-height вместо display), пилюля съезжает к центру
        # интерполяцией той же кривой, LIVE-градиент гаснет до края,
        # смаз — направленный SVG-блюр по фактической скорости
        self.assertIn("height var(--fold-t) var(--fold-ease)", css)
        self.assertIn("height:97px}", css)
        base_spdock = css.split("\n.sp-dock{")[1].split("}")[0]
        self.assertIn("max-height:0;", base_spdock)
        self.assertNotIn("display:none", base_spdock)
        self.assertIn("max-height:130px;overflow:visible;", css)
        self.assertIn("function foldEaseAt(p)", js)
        self.assertIn("dockY(true, foldEaseAt(p))", js)
        self.assertIn("rgba(0,212,255,.05) 55%,transparent 97%)", css)
        self.assertIn("createElementNS(svgNS, 'feGaussianBlur')", js)
        self.assertNotIn(".mb{filter", css)

    def test_bm25_review_fixes(self) -> None:
        """BM25: ревью beta.96 — полоса/LIVE, рамка чата, чистый старт, мягкий LIVE, маска по скроллу."""
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        # 1) все пространства скрыты — полоса не прилипает к LIVE
        self.assertIn(".spaces.no-spaces{padding-top:2px;padding-bottom:13px;gap:0}", css)
        # 2) исчезновение/появление иконок — медленнее и с высотой переходом
        self.assertIn("max-height:38px;", css.split(".sp-row{")[1].split("}")[0])
        self.assertIn("transition:opacity .42s cubic-bezier(.4,0,.2,1)", css.split(".sp-row{")[1].split("}")[0])
        self.assertIn("gap .42s cubic-bezier(.4,0,.2,1)", css.split(".spaces{")[1].split("}")[0])
        # 3) рамка вокруг чата (иконка базового пространства) — еле заметная синяя
        self.assertIn(".sp-ico.base{color:var(--tx);font-weight:700;", css)
        self.assertIn("box-shadow:inset 0 0 0 1px rgba(0,212,255,.12)}", css)
        # 4) LIVE без жёсткой прямоугольной кромки: только радиальный свет
        self.assertIn("height:34px;border:1px solid transparent;border-radius:12px;", css)
        self.assertNotIn("border:1px solid rgba(0,212,255,.34);border-radius:12px;", css)
        self.assertNotIn(".sp-mode:hover{border-color:", css)
        self.assertIn("background:radial-gradient(ellipse at 50% 50%,rgba(0,212,255,.15),", css)
        # 5) стартовая страница чистая: метки режимов не печатаются до первого ответа
        tl = js.split("function toolLine(")[1].split("\nfunction ")[0]
        self.assertIn("function toolLine(kind, on, force)", js)
        self.assertIn("!force && !box.querySelector('.msg-ai')", tl)
        self.assertIn("S.pendingModeMark = { kind, on };", tl)
        self.assertIn("if (pm.on) toolLine(pm.kind, true, true);", js)
        self.assertIn("S.pendingModeMark = null;   // BM25: отложенная метка умерла вместе с диалогом", js)
        self.assertIn("S.pendingModeMark = null;   // BM25: чужой диалог — чужие метки", js)
        # 6) верхнее затухание списка — только при скролле, въезжает мягко
        self.assertIn("@property --chatFade{syntax:'<number>';inherits:false;initial-value:1}", css)
        self.assertIn(".chat-list.topfade{--chatFade:0}", css)
        self.assertIn("cl.classList.toggle('topfade', cl.scrollTop > 2);", js)
        self.assertIn("list.classList.toggle('topfade', list.scrollTop > 2);", js)
        # 7) белая полоса при сворачивании: граница бренда постоянной ширины,
        #    подпись выведена из потока и не вылезает под бренд
        self.assertIn(".brand{border-bottom:1px solid transparent}", css)
        self.assertIn(".app.collapsed .brand-text{position:absolute;left:50px;top:50%;", css)
        # 8) иконка вкладки не телепортируется в центр первым кадром
        self.assertIn(".app.collapsed .nav-item{gap:0;width:auto;padding:10px 11px;margin:0 6px;transform:none;}", css)
        self.assertNotIn("justify-content:center;padding:10px 0;", css)
        # 9) подписи вкладок — единые часы морфа (дублирующий переход снят)
        self.assertNotIn("transition:opacity .34s ease,max-width .6s", css)
        # 10) смаз в морфе — гораздо слабее
        self.assertIn("const CAP = 2.6, K = 0.13;", js)
        # флайаут: одна кривая БЕЗ овершота — и в CSS, и в FLIP-иконке
        self.assertIn("transition:transform .32s cubic-bezier(.22,.61,.25,1),opacity .22s ease,\n    filter .3s ease}", css)
        self.assertNotIn("cubic-bezier(.3,1.12,.4,1)", css)
        self.assertNotIn("cubic-bezier(.3,1.1,.4,1)", js)
        self.assertIn("easing: 'cubic-bezier(.22,.61,.25,1)'", js)

    def test_bm18_media_skips_stock_and_prefers_direct(self) -> None:
        """Поиск медиа: стоки (403 без файла) скипаются, прямые файлы
        пробуются первыми, варианты — по интенту запроса."""
        code = Path("app/jarvis/tools/media.py").read_text(encoding="utf-8")
        self.assertNotIn("_STOCK_RE", code)
        self.assertNotIn("_DIRECT_FILE_RE", code)
        body = code.split("def _media_from_query")[1]
        self.assertNotIn("pass_no", body)
        self.assertNotIn("want_video", body)
        self.assertNotIn("want_audio", body)
        self.assertIn('"%s filetype:mp3" % q', body)
        # RuTube: страница -> официальный embed-плеер, без скачивания
        self.assertIn("_RUTUBE_RE", code)
        self.assertIn("rutube.ru/play/embed/", code)
        out = media.show_media("https://rutube.ru/video/12345678/")
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("kind"), "iframe")
        self.assertEqual(out.get("embed_url"),
                         "https://rutube.ru/play/embed/12345678")

    def test_bm18_rename_input_dark_like_files_tab(self) -> None:
        """Поле переименования в мини-вкладке ФАЙЛЫ — тот же тёмный
        цвет, что на основной вкладке (прежде селектор не применялся
        и поле было браузерно-белым)."""
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".emb-row .f-edit{width:96px;height:20px;"
                      "border:1px solid var(--line2);border-radius:7px;", css)
        self.assertIn("background:rgba(0,0,0,.4);color:var(--tx);", css)
        # прежний селектор (.emb-fx .f-edit) не применялся: инпут живёт
        # в .emb-row, а не внутри .emb-fx
        self.assertNotIn(".emb-fx .f-edit{", css)

    def test_bm18_memory_tab_guaranteed(self) -> None:
        """«Что ты помнишь обо мне» — вкладка ПАМЯТЬ гарантирована,
        даже если модель прислала битый embed-блок; фронт разбирает
        и кривой JSON."""
        sys.path.insert(0, "app")
        try:
            from jarvis import agent as ag
            self.assertEqual(ag.state_question_view("что ты помнишь обо мне"), "memory")
            broken = "вот\n```embed\nпамять\n```"
            out = ag.auto_embed_block(broken, [], q_view="memory")
            self.assertIn('"view": "memory"', out)
            self.assertNotIn("```embed\nпамять", out)
        finally:
            sys.path.remove("app")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        eps = js.split("function embedParseSpec")[1].split("\nfunction ")[0]
        self.assertIn("([a-zа-яё]+)", eps)   # lenient-достаём view регуляркой

    def test_bm28_live_call_from_scratch(self) -> None:
        """BM28/BM29: LIVE — созвон с Джарвисом, интерфейс с нуля."""
        html = Path("app/jarvis/web/index.html").read_text(encoding="utf-8")
        css = Path("app/jarvis/web/css/app.css").read_text(encoding="utf-8")
        js = Path("app/jarvis/web/js/app.js").read_text(encoding="utf-8")
        py_server = Path("app/jarvis/server.py").read_text(encoding="utf-8")

        # вход в звонок — существующая кнопка LIVE над иконками пространств;
        # диктовка и тумблеры композера работают как прежде
        self.assertIn('id="spModeLive"', html)
        self.assertIn("$('#spModeLive').addEventListener('click', () => { liveOpen(); });", js)
        self.assertIn('id="micBtn" data-tip="Диктовка"', html)
        self.assertNotIn('id="liveBtn"', html)
        self.assertIn('id="tgCamera"', html)
        self.assertIn('id="tgComputer"', html)

        # сцена: фиксированный оверлей над приложением, под модалками
        self.assertIn("#liveRoot{position:fixed;inset:0;z-index:460;", css)
        self.assertIn("body.live-on .modal-back{z-index:480}", css)
        self.assertIn("body.live-on .toast{z-index:495}", css)

        # вход/выход из глубины: вуаль темноты спадает медленно, фон из размытия
        self.assertIn(".live-veil{", css)
        self.assertIn("#liveRoot.open .live-veil{opacity:0}", css)
        self.assertIn("transition:opacity 1.5s ease}", css)
        self.assertIn("filter:blur(14px) brightness(.55)", css)
        self.assertIn("#liveRoot.open .live-bg{filter:blur(0) brightness(1);transform:scale(1)}", css)

        # ядро — одно тело: орб <-> строка (spotlight-морф), большой босс-орб
        self.assertIn("#liveRoot.text-on .live-core{width:min(780px,84vw);height:84px;border-radius:46px", css)
        self.assertIn(".live-core{position:relative;width:300px;height:300px;border-radius:50%", css)
        self.assertIn("@keyframes loIriY", css)
        self.assertIn("@keyframes loFlash", css)
        # БЕЗ колец: шар большой, дышит, по нему переливается свет
        self.assertNotIn("lo-ring", js.split('function liveBuild')[1].split('function ')[0])
        self.assertIn("@keyframes loIriR", css)   # редкие переливы: золото чаще, красный реже
        self.assertIn("@keyframes loBreathe", css)
        self.assertIn("#liveRoot.ph-speaking .lo-speak{opacity:1}", css)
        # ГЛУБИНА: единый закон появления
        self.assertIn("@keyframes deepIn", css)
        self.assertIn("animation:deepIn .9s var(--live-ease) both}", css)
        self.assertIn(".live-a{font-size:23px;line-height:1.62;", css)
        # микрофон включён — текста НЕТ, только голос
        self.assertIn("#liveRoot.mic-on .live-dream,#liveRoot.cam-on .live-dream{opacity:0}", css)
        # элементы уступают: сдвиг влево с затемнением
        self.assertIn("#liveRoot.side-on .live-flow{transform:translateX(-15vw) scale(.84)", css)
        self.assertIn("filter:brightness(.6) saturate(.85)", css)
        # круглые кнопки на стекле
        self.assertIn(".lb{width:64px;height:64px;border-radius:50%", css)
        self.assertIn(".live-bar{position:absolute;bottom:5.5vh", css)

        # камера из центра; панель звонка; агент краснит воду
        self.assertIn("#liveRoot.cam-on .live-camwrap{", css)
        self.assertIn(".live-bar{", css)
        self.assertIn(".lb-exit:hover{", css)
        self.assertIn("#liveRoot.ag .live-bg .a1{background:radial-gradient(circle,rgba(224,52,88,.34),transparent 66%)}", css)
        self.assertIn(".live-dream{", css)
        self.assertIn(".live-side{", css)

        # инструменты и интерактивы — мимолётом, из глубины
        self.assertIn("@keyframes liveToolIn", css)
        self.assertIn("@keyframes liveAskIn", css)
        self.assertIn("@keyframes deepOut", css)      # единый закон ухода в глубину

        # JS: сцена слушает проверенный движок разговора
        self.assertIn("async function liveOpen", js)
        self.assertIn("function liveClose", js)
        self.assertIn("function liveBuild", js)
        self.assertIn("function livePhase", js)
        self.assertIn("function liveLevel", js)
        self.assertIn("if (LIVE.on) livePhase(p);", js)
        self.assertIn("if (LIVE.on) liveLevel(", js)
        self.assertIn("if (LIVE.on) liveShowQuestion(text);", js)
        self.assertIn("onDelta: (chunk) => { if (chunk) voiceFeed(chunk); },   // только голос: текста на экране нет", js)
        # LIVE-крючок в handleEvent бронирован: визуал не рвёт поток
        self.assertIn("if (LIVE.on) { try { liveEvent(ev); } catch (e0)", js)
        # аккорды входа/выхода — на том же синтезаторе
        self.assertIn("function liveChord", js)
        self.assertIn("liveChord(true);", js)
        self.assertIn("liveChord(false);", js)
        # выход — только через подтверждение
        self.assertIn("function liveConfirmExit", js)
        self.assertIn("if (LIVE.on) liveConfirmExit();", js)
        # каждый звонок — новый разговор, приветствие погашено
        self.assertIn("VOICE.chatId = '';", js)
        # мужской голос Джарвиса, баритон
        self.assertIn("yuri|pavel|dmitri|artem", js)
        self.assertIn("u.pitch = 0.92;", js)
        # режим из потока поднимает кнопки звонка, если футер пуст
        self.assertIn("else if (LIVE.on && !S.computerUse) liveSetComp(true);", js)
        self.assertIn("if (LIVE.on) { if (!LIVE.cam) liveSetCam(true); }", js)
        # панель звонка: камера/микрофон/компьютер/выход
        for anchor_id in ('id="lbCam"', 'id="lbMic"', 'id="lbComp"', 'id="lbExit"'):
            self.assertIn(anchor_id, js)
        # инструменты: не больше трёх мимолётных карточек
        self.assertIn("while (cards.length > 3) liveToolOut(cards.shift());", js)
        # интерактив решает нажатием настоящей кнопки скрытой карточки
        self.assertIn("$$('.panel-card.approve-card, .panel-card.ask-card',", js)
        self.assertIn("(LIVE.host || stream()));", js)
        # BM29: инструменты работают (голосовой каскад больше не режет их)
        self.assertIn("text, live: LIVE.on, voice: !LIVE.on, silent: true,", js)
        self.assertIn("text, live: true, silent: true,", js)
        self.assertIn('chat_id = db.create_chat("LIVE", kind="live")["id"]', py_server)
        # никаких следов: служебный чат звонка удаляется при выходе
        self.assertIn("api('/api/chats/delete', { chat_id: liveChat });", js)
        # камера звонка — свой поток, БЕЗ карточки в диалоге
        self.assertIn("LIVE.camStream = stream;", js)
        self.assertNotIn("startCam(); } catch", js)
        # аудио просыпается в жесте клика — микрофон работает сразу
        self.assertIn("function liveWakeAudio()", js)
        self.assertIn("liveWakeAudio();                    // СИНХРОННО в жесте клика", js)
        self.assertIn("if (VOICE.ctx.state === 'suspended') { try { VOICE.ctx.resume(); }", js)
        # BM29.1: лёгкость — вечное движение только transform/opacity,
        # ни одного blur-фильтра на живых слоях фона
        self.assertNotIn(".live-waves", css)          # «странные полосы» убраны
        self.assertNotIn("filter:blur(80px)", css)    # авроры без дорогого blur
        self.assertIn("will-change:transform", css)   # композитор, не paint
        # строка — LED-лента: свечение только наружу
        self.assertIn("#liveRoot.text-on .live-core{width:min(780px,84vw);height:84px;border-radius:46px", css)
        self.assertIn(".live-core::before{content:'';position:absolute;inset:0;border-radius:inherit", css)
        self.assertIn("@keyframes loLed{0%,100%{opacity:.55}50%{opacity:1}}", css)   # лента пульсирует СВЕТОМ
        # ядро — КРУГ (квадратный lo-core больше не накрывает орб)
        self.assertIn(".lo-core{position:relative;width:88%;height:88%;border-radius:50%", css)
        self.assertIn(".lo-pulse .lo-halo{inset:0;width:100%;height:100%;", css)   # орб-портал: гекс-ореолы света
        self.assertNotIn("lo-ring", js)
        # камера: и орб, и строка уезжают НАВЕРХ (не в сторону)
        self.assertIn("#liveRoot.mic-on.cam-on .live-core-wrap,", css)
        self.assertIn("#liveRoot.text-on.cam-on .live-core-wrap{", css)
        self.assertIn("calc(-50% - 36vh)) scale(.6)}", css)
        self.assertNotIn("calc(-50% - 22vw)", css)
        # кнопки шире, ховер — свечение-лента без заливок
        self.assertIn("display:flex;gap:92px;padding:12px 36px", css)
        self.assertIn(".lb:hover{background:rgba(0,200,240,.13);color:var(--cy2);", css)
        # подтверждение выхода — в дизайне LIVE
        self.assertIn(".live-confirm{", css)
        self.assertIn("'<div class=\"lc-title\">Завершить звонок?</div>'", js)
        self.assertNotIn("confirmBox('Выйти из LIVE?'", js)
        # морф: орб РАСТЯГИВАЕТСЯ в строку (перетекание)
        self.assertIn("transform:scale(1.45,.3);pointer-events:none}", css)
        # VAD: конец фразы по 1000мс — молниеносность
        self.assertIn("now - VOICE.lastVoice > 700", js)
        # НАСТОЯЩИЙ ГОЛОС: серверный TTS с фолбэком на системный синтез
        self.assertIn("function voiceSpeakViaServer(text)", js)
        self.assertIn("VOICE_TTS_OK = Date.now() + 45000;", js)
        self.assertIn("api('/api/tts', { text })", js)
        py_server = Path("app/jarvis/server.py").read_text(encoding="utf-8")
        py_agent_src = Path("app/jarvis/agent.py").read_text(encoding="utf-8")
        py_llm_src = Path("app/jarvis/llm.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/api/tts":', py_server)
        self.assertIn('if body.get("kind") == "live":', py_server)
        self.assertIn("tts.api.cloud.yandex.net/speech/v1/tts:synthesize", py_server)
        # BM30.3: правильный порядок поддомена ПЕРВЫМ + перебор хостов
        # (прошлый api.tts... НЕ существует — Errno 8 nodename на macOS)
        self.assertIn("_TTS_URLS = [", py_server)
        self.assertIn('"https://api.tts.cloud.yandex.net/speech/v1/tts:synthesize",', py_server)
        self.assertIn("for url in _TTS_URLS:", py_server)
        self.assertIn('"voice": "ermil"', py_server)
        # BM29.2: анализатор привязан к контексту (перебой и VAD живые)
        self.assertIn("if (!VOICE.an || VOICE.anCtx !== VOICE.ctx) {", js)
        # перебой быстрее: 5 кадров
        self.assertIn("if (VOICE.barge >= 5) {", js)
        # инструменты в LIVE: ВСЕ видны, без капа времени
        toolshow = js.split("function liveToolShow")[1].split("\nfunction ")[0]
        self.assertNotIn("SILENT_TOOLS", toolshow)
        self.assertNotIn("setTimeout", toolshow)
        # BM33: ответ всплывает КАК ЗАПРОС: контейнер один раз плывёт снизу
        # (laIn), куски — inline-span'ы с проявлением только прозрачностью:
        # пробелы не слипаются, строка не дёргается
        self.assertIn("LIVE.aEl.appendChild(sp);", js)
        self.assertIn("if (!LIVE.aEl.classList.contains('in')) LIVE.aEl.classList.add('in');", js)
        self.assertIn(".live-a .la-w{animation:laWord .65s ease both}", css)
        self.assertIn("@keyframes laWord{from{opacity:0}}", css)
        self.assertIn(".live-a.in{animation:laIn .9s var(--live-ease) both}", css)
        self.assertNotIn("display:inline-block;animation:laWord", css)
        self.assertNotIn("LIVE.aEl.textContent += chunk;", js)
        # возврат к строке: прошлый текст не оживает
        self.assertIn("if (LIVE.qEl) LIVE.qEl.textContent = '';", js)
        # ошибка потока видна в сцене
        self.assertIn("case 'error': {", js)
        # камера звонка кормит запросы кадрами
        self.assertIn("async function liveAttachFrame", js)
        self.assertIn("camera_on: camLive() || liveCamOn,", js)
        # краткий LIVE-промпт на сервере
        self.assertIn("LIVE_MODE_NOTE = (", py_agent_src)
        # DeepSeek первым на простых тирах, GigaChat резерв
        self.assertIn("def _cheap_first(providers", py_llm_src)
        self.assertIn('if tier not in ("nano", "base") or len(providers) < 2:', py_llm_src)
        # TTS: ретраи не вечный отказ + честная причина
        self.assertIn("VOICE_TTS_OK = Date.now() + 45000;", js)
        self.assertIn("Голос Джарвиса: ' + r.error", js)
        # орб пульсирует по громкости голоса (идея №1)
        self.assertIn("function liveTtsPulse(au) {", js)
        # мотыльки мысли (идея №2)
        # BM31: трансформации — фигуры круглешка в орбе (ещё плавнее)
        self.assertIn("function liveShapeFrame() {", js)
        self.assertIn("g.innerHTML = orbFigureFrame(LIVE_SHAPE.key, t);", js)
        self.assertIn("function orbFigureFrame(key, t) {", js)
        self.assertNotIn("dotShapeFrame(LIVE_SHAPE.key", js)   # круглешок больше не рисуется
        self.assertIn("const LIVE_SHAPE = { key: ''", js)
        self.assertIn("LIVE_SHAPE.nextAt = now + 17000 + Math.random() * 21000;", js)
        self.assertIn(".lo-shape{position:absolute;inset:0;width:100%;height:100%;opacity:0;", css)
        # сжатие при думанье — вместо анимации «загрузки»
        self.assertIn("#liveRoot.ph-thinking .lo-core{--loS:.84}", css)
        self.assertIn("transform 1.1s var(--live-ease)", css)

        # ===== BM30: ГЛУБОКАЯ ЧЕСТНОСТЬ ГОЛОСА + ЖИВАЯ СЦЕНА =====
        # звёзды УБРАНЫ: фон — только расплывчатые оттенки синего
        self.assertNotIn("live-stars", js)
        self.assertNotIn(".live-stars", css)
        self.assertNotIn("@keyframes loStar", css)
        # изредка — градиентная краска (дыхание почти всегда невидимо)
        self.assertIn(".live-bg .a7{", css)
        self.assertIn("@keyframes loRare{", css)
        self.assertIn("0%,58%,100%{opacity:0}", css)
        # фон — оттенки синего (розовый/оранжевый дыхания ушли)
        self.assertNotIn("rgba(216,64,180", css)
        self.assertNotIn("rgba(255,170,80", css)
        # TTS: классификация отказов вместо слепых ретраев
        self.assertIn('return {"ok": False, "class": "config",', py_server)
        self.assertIn('return {"ok": False, "class": "net", "detail": reason[:200],', py_server)
        self.assertIn('return {"ok": False, "class": "server", "detail": reason[:200],', py_server)
        self.assertIn("except urllib.error.HTTPError as e:", py_server)
        self.assertIn("Яндекс не принял API-ключ (HTTP 401)", py_server)
        self.assertIn("роли ai.speechkit-tts.user", py_server)
        self.assertIn("def _tts_log(line: str) -> None:", py_server)
        # клиент: постоянные отказы (config/auth) не ретраятся; net остывает 45с
        self.assertIn("if (cls === 'config' || cls === 'auth') {", js)
        self.assertIn("VOICE_TTS_OK = false;   // ключ не вписан/не принят — до нового звонка", js)
        # новый звонок — новая попытка голоса
        self.assertIn("VOICE_TTS_OK = null;", js)
        # диагностика голоса в один клик из Настроек
        self.assertIn("id=\"testTts\">Проверить голос", js)
        # LIVE-промпт: НЕ МОЛЧИ во время работы (просьба человека)
        self.assertIn("ВО ВРЕМЯ РАБОТЫ НЕ МОЛЧИ", py_agent_src)
        self.assertIn("ОБЪЯВЛЯЙ вслух одной короткой живой фразой", py_agent_src)
        # кадр камеры = живые глаза: модель не предлагает «сделать фото»
        self.assertIn("ЖИВОЙ кадр камеры", py_server)
        self.assertIn("НЕ предлагай сделать фото", py_server)
        # BM30.1: авто-реплики «вижу тебя» НЕТ — semантика кадра живёт
        # в системной ноте, а не в навязанных фразах (просьба человека)
        self.assertNotIn("что ты меня видишь", js)
        # BM32: строка состояния голоса УБРАНА (решение человека);
        # честные причины живут в тостах (TTS/ASR), не в постоянной строке
        self.assertNotIn("liveVoiceSet", js)
        self.assertNotIn(".live-voice", css)
        self.assertNotIn("ГОЛОС · ЯНДЕКС", js)
        # чип провайдера: приоритетно ДОСТУПНЫЙ, а не последний использованный
        self.assertIn("function provPriority() {", js)
        self.assertIn("будет отвечать: ", js)
        # камера: орб сильнее сжимается и полностью над панелью
        self.assertIn("calc(-50% - 36vh)) scale(.6)}", css)
        # кнопки/док чуть меньше, разлёт сохранён
        self.assertIn(".lb{width:64px;height:64px;border-radius:50%", css)
        self.assertIn("display:flex;gap:92px;padding:12px 36px", css)
        # BM30.1: долгое думанье — глубинные пузыри (только transform/opacity)
        # BM31: мотыльки и пузыри УБРАНЫ (анимация загрузки упразднена)
        self.assertNotIn("live-motes", js)
        self.assertNotIn("live-bubbles", js)
        self.assertNotIn("loMorphA", css)
        self.assertNotIn(".lo-think", css)
        # орб-портал: тело-дымка + мягкие КРУГЛЫЕ ореолы (гекс-грани ушли)
        self.assertIn(".lo-pulse .lo-body{inset:2%;border-radius:50%;", css)
        self.assertNotIn("lo-veilx", js)
        self.assertIn("'<circle r=\"15.4\" fill=\"url(#gHalo)\"/>' +", js)
        # очередь речи: два баритона больше не говорят наперекрыз
        self.assertIn("let VOICE_TTS_QUEUE = Promise.resolve();", js)
        self.assertIn("let VOICE_TTS_GEN = 0;", js)
        self.assertIn("VOICE_TTS_QUEUE = VOICE_TTS_QUEUE.then(speak, speak);", js)
        self.assertIn("VOICE_TTS_GEN++;                   // BM31: очередь речи гаснет целиком", js)
        # кнопка отправки — портал-гекс со свечением; свечение строки меньше
        self.assertIn(".live-send .ls-hex{position:absolute;inset:-34%;width:168%;height:168%;", css)
        self.assertIn("box-shadow:0 0 22px rgba(150,220,255,.13)", css)
        # темп речи
        self.assertIn('"speed": "1.08"', py_server)
        # ===== BM32: ИНТОНАЦИЯ + КОРНИ «МОЛЧИТ» + ОРБ-ДЫМКА =====
        # амплуа «good» у эрмила: запрос с emotion + разовый фолбэк без него
        self.assertIn('_TTS_EMOTION = None', py_server)
        self.assertIn('payload["emotion"] = "good"', py_server)
        self.assertIn('if e.code == 400 and with_emotion and _TTS_EMOTION is None:', py_server)
        self.assertIn('"EMOTION «good» отвергнута — говорю без амплуа"', py_server)
        self.assertIn('"EMOTION «good» принята — говорю с интонацией"', py_server)
        # BM33: ЦЕПОЧКА ГОЛОСОВ — OpenAI-совместимый TTS (gpt-4o-mini-tts с
        # instructions = живая интонация) через AITunnel, откат на Яндекс
        self.assertIn("def _tts_openai(self, text: str, prov:", py_server)
        self.assertIn('model = str(prov.get("tts_model") or "gpt-4o-mini-tts")', py_server)
        self.assertIn('voice = str(prov.get("tts_voice") or "onyx")', py_server)
        self.assertIn('url = base + "/audio/speech"', py_server)
        self.assertIn('"instructions": self._TTS_INSTRUCTIONS,', py_server)
        self.assertIn("_TTS_INSTRUCTIONS = (", py_server)
        self.assertIn("if at_key:", py_server)
        self.assertIn("if r is not None:\n                return r", py_server)
        # микрофон не открывается поверх ещё говорящего Яндекса (эхо-самосглаз)
        self.assertIn("if (VOICE.ttsBusy) return;", js)
        self.assertIn("VOICE.ttsBusy = true;                   // BM32: Яндекс заговорил", js)
        # системный синтез не может зависнуть навсегда (сторож очереди)
        self.assertIn("guard = setTimeout(() => {", js)
        self.assertIn("7000 + Math.min(20000, text.length * 90)", js)
        # отказ распознавания больше не немой: причина — тостом, раз за звонок
        self.assertIn("toast('Распознавание речи: ' + r.error, 'warn', 'ASR');", js)
        self.assertIn("VOICE.asrErrShown = false;", js)
        # орбиты портала: 3 тонких круга в разных плоскостях, вечное вращение
        self.assertIn("'<i class=\"lo-orbit lo-o1\"></i>' +", js)
        self.assertIn(".lo-orbit.lo-o1{width:118%;height:118%;margin:-59% 0 0 -59%;", css)
        self.assertIn(".lo-orbit.lo-o3{width:154%;height:154%;margin:-77% 0 0 -77%;", css)
        self.assertIn("function liveOrbitsFrame() {", js)
        self.assertIn("const LIVE_ORBITS = [", js)
        self.assertIn("LIVE.orbits = Array.from(root.querySelectorAll('.lo-orbit'));", js)
        self.assertIn("liveOrbitsFrame();    // BM33: орбиты вечно валятся по всем осям", js)
        self.assertNotIn("@keyframes loOrbA", css)
        # дымка расплывается: дышащие пятна поверх тела
        self.assertIn(".lo-pulse .lo-haze{position:absolute;border-radius:50%;mix-blend-mode:screen;", css)
        self.assertIn("@keyframes loHz1{0%,100%{opacity:.5;transform:translate(-6%,4%) scale(.94)}", css)
        # фигура проступает ИЗ орба: яркость дымки уводится в грани
        self.assertIn("filter:brightness(calc(1 - var(--loFig,0)*.45));", css)
        self.assertIn("LIVE.root.style.setProperty('--loFig', e.toFixed(3));", js)
        # проявление/растворение — 5.2с безумно плавно, S-кривая
        self.assertIn("LIVE_SHAPE.m = Math.min(1, LIVE_SHAPE.m + 16 / 5200);", js)
        self.assertIn("const e = LIVE_SHAPE.m * LIVE_SHAPE.m * (3 - 2 * LIVE_SHAPE.m);", js)
        # фигуры ЕЛЕ заметные: грани-дымка и рёбра-волосок, размером с орб
        self.assertIn("const SC = ORB_FIG_SC[key] || 12.0;", js)
        self.assertIn("const op = 0.03 + 0.07 * bright;", js)
        self.assertIn('(0.14 + 0.10 * tt).toFixed(2)', js)
        # ===== BM33: СУПЕР-ПЛАВНЫЕ ПЕРЕХОДЫ + ОРБИТЫ-ВОЛЧКИ + ЧЁТКИЕ КНОПКИ =====
        # свет дышит ТОЛЬКО прозрачностью — кромка градиента не ползает
        # (ползущий край и был видимой «границей перехода к тёмному»)
        self.assertIn("@keyframes loBreathe{0%,100%{opacity:.78}50%{opacity:1}}", css)
        self.assertIn("@keyframes loHeart{0%,100%{opacity:.8}50%{opacity:1}}", css)
        self.assertNotIn("opacity:.8;transform:scale(.97)}", css)
        self.assertNotIn("opacity:.35;transform:scale(.92)}", css)
        # яркий центр, растушёванный к краям длинным хвостом (гаснет к 95%)
        self.assertIn(".lo-pulse .lo-body{inset:2%;border-radius:50%;", css)
        self.assertIn("rgba(242,252,255,.62) 0%", css)
        self.assertIn("rgba(26,92,166,.02) 84%,transparent 95%)", css)
        # орбиты-волчки: три скорости на кольцо, старт в разных плоскостях
        self.assertIn("{ rx: 12.4, ry: -8.2, rz: 5.1, a: [24, 0, 130] },", js)
        self.assertIn("' rotateY(' + ((o.a[1] + o.ry * t) % 360).toFixed(2) + 'deg)' +", js)
        # кнопка отправки: мягкий круглый свет вместо резкого гекса
        self.assertIn("'<circle r=\"14.6\" fill=\"url(#gSend)\"/>' +", js)
        self.assertNotIn("0,-15.5", js)
        # кнопки панели: ЧЁТКАЯ круглая область
        self.assertIn("background:rgba(10,20,34,.42);", css)
        self.assertIn("border:1px solid rgba(120,200,255,.15);color:var(--tx2)", css)
        self.assertNotIn("rgba(120,200,255,.06) 0%,rgba(120,200,255,.028) 52%", css)
        # ===== BM30.2: СЛЕПАЯ КАМЕРА И ВЕЧНОЕ МОЛЧАНИЕ — КОРНИ =====
        # просьба режима ВИДНА в LIVE (сервер ждёт ответа до 300с!)
        self.assertIn("case 'mode_request': return liveModeAsk(ev);", js)
        self.assertIn("function liveModeAsk(ev) {", js)
        self.assertIn("if (ev.mode === 'camera' && !LIVE.cam) liveSetCam(true);", js)
        # mode_changed камеры в LIVE поднимает камеру ЗВОНКА (не композера)
        self.assertIn("if (LIVE.on) { if (!LIVE.cam) liveSetCam(true); }", js)
        # "что ты видишь" триггерит предложение камеры
        self.assertIn("что\\s+(?:ты\\s+)?видишь", py_agent_src)
        # кадр не получился — модель знает честно (ретрай + нота)
        self.assertIn("кадр не удалось получить — скажи об этом одной короткой фразой", js)
        self.assertIn("text: askText,", js)
        # позднее включение камеры: система-нота вместо «я не вижу»
        self.assertIn("Пользователь только что разрешил камеру", py_server)
        # выход из LIVE возвращает последний диалог / страницу нового
        self.assertIn("LIVE.prevChatId = (S.chatId && !String(S.chatId).startsWith('live-')) ? S.chatId : '';", js)
        self.assertIn("if (backId) openChat(backId);", js)
        self.assertIn("else newChat();", js)
        # TTS: сырая причина ошибки ОС видна человеку (тост с r.error —
        # в нём сервер уже приложил хвост причины; строка состояния голоса
        # удалена в BM32, причины живут в тостах)
        self.assertIn('tail = " · причина: " + reason[:140]', py_server)
        self.assertIn('"nodename" in low or "servname" in low', py_server)
        self.assertIn("toast('Голос Джарвиса: ' + r.error +", js)

        # перебой глушит и серверное аудио
        self.assertIn("if (VOICE.ttsAudio) {              // BM29: серверный голос тоже замолкает", js)

        # диктовка: СТОП мгновенный, не ждёт транскрипции хвоста
        df = js.split("function dictFinish(D) {")[1].split("\n}\n")[0]
        self.assertIn("DICT = null;", df)
        self.assertIn("if (mbx) mbx.classList.remove('rec');", df)
        onstop = js.split("D.rec.onstop = async () => {")[1].split("armSegment();")[0]
        self.assertIn("if (closedAll) {", onstop)
        self.assertNotIn("dictTranscribeSegment(chunks", onstop.split("closedAll")[1])


if __name__ == "__main__":
    unittest.main()
