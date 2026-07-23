"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import asyncio
import importlib.machinery
import os
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# numpy ships only in the optional "voice" extra (not [all,dev]); the mixer
# math needs it, so skip this whole module when it isn't installed.
np = pytest.importorskip("numpy")

# voice_mixer lives inside the discord plugin package dir; import by path the
# same way the adapter does.
_DISCORD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "platforms", "discord",
)
if _DISCORD_DIR not in sys.path:
    sys.path.insert(0, _DISCORD_DIR)

import voice_mixer as vm  # noqa: E402


# =====================================================================
# Pure mixer unit tests
# =====================================================================

class TestVoiceMixerCore:
    def test_is_discord_audio_source(self):
        if importlib.machinery.PathFinder.find_spec("discord") is None:
            pytest.skip("discord.py is not installed")

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import discord; "
                    "from plugins.platforms.discord.voice_mixer import VoiceMixer; "
                    "assert isinstance(VoiceMixer(), discord.AudioSource)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr

    def test_frame_geometry_matches_discord(self):
        # 20ms @ 48kHz stereo s16 == 3840 bytes (discord.opus.Encoder.FRAME_SIZE)
        assert vm.FRAME_SIZE == 3840
        assert vm.SAMPLES_PER_FRAME == 960
        assert len(vm.SILENCE_FRAME) == vm.FRAME_SIZE

    def test_empty_mixer_returns_silence_frames(self):
        mx = vm.VoiceMixer()
        for _ in range(5):
            frame = mx.read()
            assert len(frame) == vm.FRAME_SIZE
            assert frame == vm.SILENCE_FRAME

    def test_is_opus_false(self):
        # discord.py sends raw PCM when is_opus() is False.
        assert vm.VoiceMixer().is_opus() is False

    def test_ambient_loops_and_is_quiet(self):
        mx = vm.VoiceMixer(ambient_gain=0.2)
        amb = vm.synth_ambient_pcm(seconds=0.5)
        assert len(amb) % vm.FRAME_SIZE == 0  # frame-aligned for seamless loop
        mx.set_ambient(amb)
        peaks = [int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                 for _ in range(100)]  # 2s >> 0.5s loop
        # Produces audio after the fade-in and stays under the configured gain.
        assert any(p > 0 for p in peaks[10:])
        assert max(peaks) < int(32767 * 0.5)


# =====================================================================
# Adapter integration
# =====================================================================

def _make_adapter(fx_cfg=None):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig
    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._ambient_pcm_cache = None
    adapter._ack_pcm_cache = {}
    adapter._ack_pcm_locks = {}
    adapter._ack_prewarm_task = None
    adapter._voice_fx_cfg = fx_cfg if fx_cfg is not None else {
        "enabled": True, "ambient_enabled": True, "ambient_path": "",
        "ambient_gain": 0.18, "duck_gain": 0.06, "speech_gain": 1.0,
        "ack_enabled": True, "ack_phrases": ["One moment."],
    }
    return adapter


class TestVoiceMixerActive:


    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestPlayInVoiceChannelMixerPath:
    @pytest.mark.asyncio
    async def test_routes_through_mixer_when_present(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc

        # speech_active returns True once (so play_speech is observed) then
        # False so the wait loop exits promptly.
        class _Mixer:
            def __init__(self):
                self._polls = 0
                self.play_speech = MagicMock()

            @property
            def speech_active(self):
                self._polls += 1
                return self._polls <= 1

        mixer = _Mixer()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        fake_pcm = b"\x00" * vm.FRAME_SIZE
        with patch.object(vm, "decode_to_pcm", return_value=fake_pcm):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        assert ok is True
        mixer.play_speech.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(111)
        # Legacy path must NOT have been used.
        vc.play.assert_not_called()


class TestLeadSilence:
    """Warm-up lead silence prepended to speech so the first word isn't clipped
    (issue #66827)."""

    def test_bytes_empty_when_unset(self):
        adapter = _make_adapter()  # default cfg has no lead_silence_ms
        assert adapter._lead_silence_bytes() == b""


    def test_bytes_length_matches_ms(self):
        adapter = _make_adapter({"lead_silence_ms": 200})
        lead = adapter._lead_silence_bytes()
        assert lead == b"\x00" * (vm.BYTES_PER_MS * 200)
        assert len(lead) == 200 * 192  # 48kHz stereo s16 -> 192 bytes/ms


class TestPlayAckInVoice:
    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False


    @pytest.mark.asyncio
    async def test_plays_speech_when_armed(self, tmp_path):
        adapter = _make_adapter()
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        ack_file = tmp_path / "ack.mp3"
        ack_file.write_bytes(b"id3")
        import json as _json
        with patch("tools.tts_tool.text_to_speech_tool",
                   return_value=_json.dumps({"success": True, "file_path": str(ack_file)})), \
                patch.object(vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE):
            ok = await adapter.play_ack_in_voice(111, phrase="Testing one two.")
        assert ok is True
        mixer.play_speech.assert_called_once()
        assert not ack_file.exists()

    @pytest.mark.asyncio
    async def test_decode_failure_removes_provider_output(self, tmp_path):
        adapter = _make_adapter()
        adapter._voice_mixers[111] = MagicMock()

        ack_file = tmp_path / "bad-ack.mp3"
        ack_file.write_bytes(b"not audio")
        import json as _json
        with patch(
            "tools.tts_tool.text_to_speech_tool",
            return_value=_json.dumps({"success": True, "file_path": str(ack_file)}),
        ), patch.object(vm, "decode_to_pcm", return_value=None):
            ok = await adapter.play_ack_in_voice(111, phrase="Decode failure.")

        assert ok is False
        assert not ack_file.exists()

    @pytest.mark.asyncio
    async def test_malformed_provider_path_does_not_leak_requested_file(self):
        adapter = _make_adapter()
        adapter._voice_mixers[111] = MagicMock()
        created_paths = []

        def malformed_output(*, text, output_path):
            del text
            created_paths.append(output_path)
            with open(output_path, "wb") as audio_file:
                audio_file.write(b"id3")
            return '{"success": true, "file_path": []}'

        with patch(
            "tools.tts_tool.text_to_speech_tool", side_effect=malformed_output
        ):
            ok = await adapter.play_ack_in_voice(111, phrase="Malformed path.")

        assert ok is False
        assert created_paths
        assert all(not os.path.exists(path) for path in created_paths)

    @pytest.mark.asyncio
    async def test_reuses_cached_pcm_for_same_phrase(self, tmp_path):
        adapter = _make_adapter()
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        ack_file = tmp_path / "ack.mp3"
        ack_file.write_bytes(b"id3")
        import json as _json
        with patch(
            "tools.tts_tool.text_to_speech_tool",
            return_value=_json.dumps({"success": True, "file_path": str(ack_file)}),
        ) as synthesize, patch.object(
            vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE
        ) as decode:
            first = await adapter.play_ack_in_voice(111, phrase="Already warm.")
            second = await adapter.play_ack_in_voice(111, phrase="Already warm.")

        assert first is True
        assert second is True
        assert synthesize.call_count == 1
        assert decode.call_count == 1
        assert mixer.play_speech.call_count == 2

    @pytest.mark.asyncio
    async def test_resynthesizes_same_phrase_when_tts_config_changes(self):
        adapter = _make_adapter()
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        def synthesize(*, text, output_path):
            del text
            with open(output_path, "wb") as audio_file:
                audio_file.write(b"id3")
            return '{"success": true, "file_path": "%s"}' % output_path

        active_voice = {"name": "voice-a"}

        def load_config():
            return {
                "tts": {
                    "provider": "edge",
                    "edge": {"voice": active_voice["name"]},
                }
            }

        with patch(
            "hermes_cli.config.load_config", side_effect=load_config
        ), patch(
            "tools.tts_tool.text_to_speech_tool", side_effect=synthesize
        ) as synthesis, patch.object(
            vm,
            "decode_to_pcm",
            side_effect=[b"\x01" * vm.FRAME_SIZE, b"\x02" * vm.FRAME_SIZE],
        ) as decode:
            first = await adapter.play_ack_in_voice(111, phrase="Config-aware.")
            active_voice["name"] = "voice-b"
            second = await adapter.play_ack_in_voice(111, phrase="Config-aware.")

        assert first is True
        assert second is True
        assert synthesis.call_count == 2
        assert decode.call_count == 2
        assert mixer.play_speech.call_args_list == [
            call(b"\x01" * vm.FRAME_SIZE, gain=1.0),
            call(b"\x02" * vm.FRAME_SIZE, gain=1.0),
        ]
        assert len(adapter._ack_pcm_cache) == 1
        assert len(adapter._ack_pcm_locks) == 1

    @pytest.mark.asyncio
    async def test_config_change_during_synthesis_is_not_cached_under_old_scope(self):
        adapter = _make_adapter()
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()
        active_voice = {"name": "voice-a"}
        synthesis_count = 0

        def load_config():
            return {
                "tts": {
                    "provider": "edge",
                    "edge": {"voice": active_voice["name"]},
                }
            }

        def synthesize(*, text, output_path):
            nonlocal synthesis_count
            del text
            synthesis_count += 1
            if synthesis_count == 1:
                active_voice["name"] = "voice-b"
            with open(output_path, "wb") as audio_file:
                audio_file.write(active_voice["name"].encode())
            return '{"success": true, "file_path": "%s"}' % output_path

        with patch(
            "hermes_cli.config.load_config", side_effect=load_config
        ), patch(
            "tools.tts_tool.text_to_speech_tool", side_effect=synthesize
        ) as synthesis, patch.object(
            vm, "decode_to_pcm", side_effect=lambda path: open(path, "rb").read()
        ):
            first = await adapter.play_ack_in_voice(111, phrase="Race-safe.")
            active_voice["name"] = "voice-a"
            second = await adapter.play_ack_in_voice(111, phrase="Race-safe.")

        assert first is True
        assert second is True
        assert synthesis.call_count == 2
        assert mixer.play_speech.call_args_list == [
            call(b"voice-b", gain=1.0),
            call(b"voice-a", gain=1.0),
        ]

    @pytest.mark.asyncio
    async def test_concurrent_prewarm_and_play_share_one_synthesis(self):
        phrase = "Share this synthesis."
        adapter = _make_adapter({
            "enabled": True,
            "ambient_enabled": False,
            "ambient_gain": 0.18,
            "duck_gain": 0.06,
            "speech_gain": 1.0,
            "ack_enabled": True,
            "ack_phrases": [phrase],
        })
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        synthesis_started = threading.Event()
        release_synthesis = threading.Event()

        def synthesize(*, text, output_path):
            synthesis_started.set()
            release_synthesis.wait()
            with open(output_path, "wb") as audio_file:
                audio_file.write(text.encode())
            return '{"success": true, "file_path": "%s"}' % output_path

        prewarm_task = None
        play_task = None
        with patch(
            "tools.tts_tool.text_to_speech_tool",
            side_effect=synthesize,
        ) as synthesis, patch.object(
            vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE
        ):
            try:
                prewarm_task = asyncio.create_task(adapter._prewarm_ack_pcm())
                assert await asyncio.to_thread(synthesis_started.wait, 2)
                play_task = asyncio.create_task(
                    adapter.play_ack_in_voice(111, phrase=phrase)
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                assert synthesis.call_count == 1
            finally:
                release_synthesis.set()
                results = await asyncio.gather(
                    *(task for task in (prewarm_task, play_task) if task is not None),
                    return_exceptions=True,
                )

        assert results == [None, True]
        assert synthesis.call_count == 1
        mixer.play_speech.assert_called_once()


class TestAckPrewarm:
    @pytest.mark.asyncio
    async def test_join_does_not_wait_for_ack_synthesis(self):
        adapter = _make_adapter({
            "enabled": True,
            "ambient_enabled": False,
            "ambient_gain": 0.18,
            "duck_gain": 0.06,
            "speech_gain": 1.0,
            "ack_enabled": True,
            "ack_phrases": ["Warm this in the background."],
        })
        adapter._allowed_user_ids = set()
        adapter._voice_input_cfg = {
            "silence_threshold_seconds": 1.5,
            "min_speech_seconds": 0.5,
        }
        adapter._voice_input_callback = None
        adapter._reset_voice_timeout = MagicMock()

        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.disconnect = AsyncMock()
        voice_client._connection.secret_key = [0] * 32
        voice_client._connection.dave_session = None
        voice_client._connection.ssrc = 123
        voice_client._connection.hook = None

        channel = MagicMock()
        channel.guild.id = 111
        channel.connect = AsyncMock(return_value=voice_client)

        synthesis_started = threading.Event()
        release_synthesis = threading.Event()

        def slow_synthesis(**_kwargs):
            synthesis_started.set()
            release_synthesis.wait()
            return '{"success": false}'

        join_task = None
        try:
            with patch(
                "tools.tts_tool.text_to_speech_tool",
                side_effect=slow_synthesis,
            ):
                join_task = asyncio.create_task(adapter.join_voice_channel(channel))
                assert await asyncio.to_thread(synthesis_started.wait, 2)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                assert join_task.done()
                assert join_task.result() is True
        finally:
            release_synthesis.set()
            if join_task is not None:
                await join_task
            if 111 in adapter._voice_clients:
                await adapter.leave_voice_channel(111)

    @pytest.mark.asyncio
    async def test_prewarm_covers_every_randomly_selectable_phrase(self):
        adapter = _make_adapter({
            "enabled": True,
            "ambient_enabled": False,
            "ambient_gain": 0.18,
            "duck_gain": 0.06,
            "speech_gain": 1.0,
            "ack_enabled": True,
            "ack_phrases": ["First phrase.", "Second phrase."],
        })
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        def synthesize(*, text, output_path):
            with open(output_path, "wb") as audio_file:
                audio_file.write(text.encode())
            return '{"success": true, "file_path": "%s"}' % output_path

        with patch(
            "tools.tts_tool.text_to_speech_tool",
            side_effect=synthesize,
        ) as synthesis, patch.object(
            vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE
        ):
            await adapter._prewarm_ack_pcm()

        with patch(
            "tools.tts_tool.text_to_speech_tool",
            side_effect=AssertionError("selected phrase was not prewarmed"),
        ), patch("random.choice", return_value="Second phrase."):
            played = await adapter.play_ack_in_voice(111)

        assert played is True
        assert synthesis.call_count == 2
        mixer.play_speech.assert_called_once()

    @pytest.mark.asyncio
    async def test_prewarm_failure_does_not_prevent_mixer_install(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_playing.return_value = False
        mixer = MagicMock()

        with patch.object(vm, "VoiceMixer", return_value=mixer), \
                patch.object(adapter, "_get_ambient_pcm", return_value=None), \
                patch.object(
                    adapter,
                    "_get_ack_pcm",
                    new=AsyncMock(side_effect=RuntimeError("TTS unavailable")),
                ):
            await adapter._install_voice_mixer(111, vc)

        assert adapter._voice_mixers[111] is mixer
        vc.play.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_cancels_owned_prewarm_task(self):
        adapter = _make_adapter()
        adapter._client = None
        adapter._post_connect_task = None
        adapter._running = True
        adapter._ready_event = MagicMock()
        adapter._release_platform_lock = MagicMock()
        adapter._cancel_liveness_task = AsyncMock()
        adapter._cancel_bot_task = AsyncMock()

        never_finishes = asyncio.Event()
        prewarm_task = asyncio.create_task(never_finishes.wait())
        adapter._ack_prewarm_task = prewarm_task

        try:
            await adapter.disconnect()
            assert prewarm_task.cancelled()
            assert adapter._ack_prewarm_task is None
        finally:
            if not prewarm_task.done():
                prewarm_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await prewarm_task

    @pytest.mark.asyncio
    async def test_disconnect_during_prewarm_cleans_worker_output(self):
        phrase = "Clean this after cancellation."
        adapter = _make_adapter({
            "enabled": True,
            "ambient_enabled": False,
            "ambient_gain": 0.18,
            "duck_gain": 0.06,
            "speech_gain": 1.0,
            "ack_enabled": True,
            "ack_phrases": [phrase],
        })
        adapter._client = None
        adapter._post_connect_task = None
        adapter._running = True
        adapter._ready_event = MagicMock()
        adapter._release_platform_lock = MagicMock()
        adapter._cancel_liveness_task = AsyncMock()
        adapter._cancel_bot_task = AsyncMock()

        synthesis_started = threading.Event()
        release_synthesis = threading.Event()
        synthesis_finished = threading.Event()
        output_paths = []

        def slow_synthesis(*, text, output_path):
            del text
            output_paths.append(output_path)
            synthesis_started.set()
            release_synthesis.wait(2)
            with open(output_path, "wb") as audio_file:
                audio_file.write(b"id3")
            synthesis_finished.set()
            return '{"success": true, "file_path": "%s"}' % output_path

        with patch(
            "tools.tts_tool.text_to_speech_tool", side_effect=slow_synthesis
        ), patch.object(vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE):
            adapter._schedule_ack_prewarm()
            assert await asyncio.to_thread(synthesis_started.wait, 2)
            await adapter.disconnect()
            release_synthesis.set()
            assert await asyncio.to_thread(synthesis_finished.wait, 2)
            for _ in range(20):
                if output_paths and not os.path.exists(output_paths[0]):
                    break
                await asyncio.sleep(0.01)

        assert output_paths
        assert not os.path.exists(output_paths[0])
