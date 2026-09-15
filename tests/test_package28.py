"""Regression tests for package 28: AUTO has one server-side source of truth."""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from jarvis import agent, auto, config, server  # noqa: E402
from jarvis.tools import media  # noqa: E402

_BUILD_SPEC = importlib.util.spec_from_file_location("jarvis_installer_build", ROOT / "install" / "build.py")
assert _BUILD_SPEC and _BUILD_SPEC.loader
installer_build = importlib.util.module_from_spec(_BUILD_SPEC)
_BUILD_SPEC.loader.exec_module(installer_build)


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
    def test_rebuild_preserves_previous_embedded_keys_without_a_keys_file(self) -> None:
        cloud, deep = "cloud-fixture", "deep-fixture"
        setup = ('"$PY" - "$HOME_DIR" "%s" "%s" <<\'PYSETUP\'\n' % (
            base64.b64encode(cloud.encode()).decode(),
            base64.b64encode(deep.encode()).decode(),
        ))
        with tempfile.TemporaryDirectory() as td:
            old_out = Path(td) / "JARVIS.command"
            old_out.write_text("#!/bin/bash\n" + setup, encoding="utf-8")
            with mock.patch.object(installer_build, "OUT", old_out):
                self.assertEqual(installer_build._keys_from_previous_installer(), (cloud, deep))

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


class BrowserImageRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        with agent._BROWSER_IMAGE_COND:
            agent._BROWSER_IMAGE_PENDING.clear()

    def tearDown(self) -> None:
        with agent._BROWSER_IMAGE_COND:
            agent._BROWSER_IMAGE_PENDING.clear()

    def test_fast_completion_before_wait_is_lossless_and_single_use(self) -> None:
        request_id = agent.begin_browser_image_request("chat-fast")
        expected = {"ok": True, "path": "render.png"}
        self.assertEqual(agent.browser_image_chat(request_id), "chat-fast")
        self.assertTrue(agent.complete_browser_image_request(request_id, expected))
        self.assertFalse(agent.complete_browser_image_request(request_id, expected),
                         "a completed request must reject a second result")
        self.assertEqual(agent.wait_browser_image_request(request_id, timeout=0), expected,
                         "completion arriving before wait() must not be lost")
        self.assertIsNone(agent.browser_image_chat(request_id),
                          "wait() consumes and closes the pending request")

    def test_completion_claim_is_atomic_before_any_upload(self) -> None:
        request_id = agent.begin_browser_image_request("chat-race")
        self.assertEqual(agent.claim_browser_image_request(request_id), "chat-race")
        self.assertIsNone(agent.claim_browser_image_request(request_id),
                          "a concurrent duplicate POST must lose before writing a file")
        expected = {"ok": False, "error": "invalid image"}
        self.assertTrue(agent.complete_browser_image_request(request_id, expected))
        self.assertEqual(agent.wait_browser_image_request(request_id, timeout=0), expected)

    def _handler(self, body: dict) -> mock.Mock:
        handler = mock.Mock()
        handler.path = "/api/images/complete"
        handler._body.return_value = body
        return handler

    def test_server_binds_png_to_registry_chat_and_rejects_duplicate_post(self) -> None:
        request_id = agent.begin_browser_image_request("trusted-chat")
        handler = self._handler({
            "id": request_id,
            "chat_id": "forged-chat",
            "name": "render.png",
            "data": "unused-in-mocked-upload",
            "prompt": "a precise scene",
            "model": "gpt-image-2",
            "quality": "medium",
        })
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1200
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "render.png"

            def upload(body: dict) -> dict:
                self.assertEqual(body["chat_id"], "trusted-chat",
                                 "browser-provided chat_id must never own the file")
                target.write_bytes(png)
                return {"ok": True, "name": "render.png", "size": len(png),
                        "kind": "image", "download_url": "/api/download/render.png"}

            handler._upload.side_effect = upload
            with mock.patch.object(server.sandbox, "safe_path", return_value=target):
                server.Handler.do_POST(handler)
                first = handler._json.call_args.args
                self.assertEqual(first[1], 200)
                self.assertTrue(first[0]["ok"])
                self.assertEqual(first[0]["result"]["path"], "render.png")
                self.assertFalse(first[0]["result"]["watermark"])

                handler._json.reset_mock()
                server.Handler.do_POST(handler)
                second = handler._json.call_args.args
                self.assertEqual(second[1], 404)
                self.assertEqual(handler._upload.call_count, 1,
                                 "duplicate completion must be rejected before upload")

        result = agent.wait_browser_image_request(request_id, timeout=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-image-2")

    def test_invalid_browser_payload_is_removed_and_wakes_waiter(self) -> None:
        request_id = agent.begin_browser_image_request("chat-invalid")
        handler = self._handler({"id": request_id, "name": "fake.png", "data": "unused"})
        bad = b"not an image" * 100
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "fake.png"
            target.write_bytes(bad)
            handler._upload.return_value = {
                "ok": True, "name": "fake.png", "size": len(bad),
                "kind": "image", "download_url": "/api/download/fake.png",
            }

            def remove(_name: str, _chat_id: str) -> dict:
                target.unlink(missing_ok=True)
                return {"ok": True}

            with mock.patch.object(server.sandbox, "safe_path", return_value=target), \
                 mock.patch.object(server.sandbox, "remove", side_effect=remove) as removed:
                server.Handler.do_POST(handler)
            removed.assert_called_once_with("fake.png", "chat-invalid")
            self.assertFalse(target.exists())

        response, status = handler._json.call_args.args
        self.assertEqual(status, 400)
        self.assertFalse(response["ok"])
        result = agent.wait_browser_image_request(request_id, timeout=0)
        self.assertFalse(result["ok"])
        self.assertIn("не изображение", result["error"])


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
    def _tool_turn(call_id: str) -> list[dict]:
        return [{
            "type": "done",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "generate_image",
                    "arguments": json.dumps({"prompt": "blue glass city", "width": 1024}),
                },
            }],
        }]

    def test_media_tool_selects_gpt_image_2_without_network_or_watermark(self) -> None:
        with mock.patch.object(media.CONFIG, "get", return_value="puter"), \
             mock.patch.object(media, "_art_prompt", return_value="enhanced scene"):
            result = media.generate_image("город", width=99999, height=12)
        self.assertEqual(result, {
            "browser_image": True,
            "prompt": "enhanced scene",
            "width": 1536,
            "height": 256,
            "model": "gpt-image-2",
            "quality": "medium",
        })
        self.assertEqual(config.DEFAULTS["media"]["image_provider"], "puter")
        migrated = config._migrate({"media": {
            "image_provider": "pollinations", "image_base": "legacy", "image_model": "sana",
        }})["media"]
        self.assertEqual(migrated["image_provider"], "puter")
        self.assertNotIn("image_base", migrated)
        self.assertNotIn("image_model", migrated)
        self.assertEqual(config._migrate({"media": {"image_provider": "off"}})
                         ["media"]["image_provider"], "off")

    def test_agent_emits_handoff_then_waits_for_persisted_image(self) -> None:
        streams = iter((
            self._tool_turn("image-1"),
            [{"type": "delta", "text": "Изображение готово."},
             {"type": "done", "tool_calls": []}],
        ))
        saved = {
            "ok": True, "path": "gpt.png", "name": "gpt.png", "size": 2048,
            "download_url": "/api/download/gpt.png", "preview_url": "/api/download/gpt.png",
        }
        with mock.patch.object(agent.orchestrator, "choose_tier", return_value=self.ROUTE), \
             mock.patch.object(agent.llm, "chat_stream", side_effect=lambda *_a, **_k: next(streams)), \
             mock.patch.object(agent.tools, "schemas", return_value=self.SCHEMA), \
             mock.patch.object(agent.tools, "call", return_value={
                 "browser_image": True, "prompt": "enhanced scene", "width": 1024,
                 "height": 1024, "model": "gpt-image-2", "quality": "medium",
             }), \
             mock.patch.object(agent, "begin_browser_image_request", return_value="img-request") as begin, \
             mock.patch.object(agent, "wait_browser_image_request", return_value=saved) as wait:
            events = list(agent.Agent(chat_id="image-chat").run(
                [{"role": "user", "content": "Нарисуй город"}],
                user_text="Нарисуй город",
            ))

        begin.assert_called_once_with("image-chat")
        wait.assert_called_once_with("img-request")
        request = next(event for event in events if event.get("type") == "image_request")
        self.assertEqual(request["id"], "img-request")
        self.assertEqual(request["model"], "gpt-image-2")
        self.assertEqual(request["quality"], "medium")
        files = [event for event in events if event.get("type") == "file"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "gpt.png")
        self.assertLess(events.index(request), events.index(files[0]),
                        "file/done cannot overtake the browser generation request")

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


if __name__ == "__main__":
    unittest.main()
