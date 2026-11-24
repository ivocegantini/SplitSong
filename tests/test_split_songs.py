"""Testes do split_songs.py.

Rodar (da raiz do repositório):
    python -m unittest discover -s tests -v

Os testes de integração precisam do ffmpeg/ffprobe no PATH e são pulados se
eles faltarem. Com SPLITSONG_REQUIRE_FFMPEG=1 (usado no GitHub Actions) a
falta do ffmpeg faz os testes falharem em vez de serem pulados.
"""

import contextlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import split_songs as ss  # noqa: E402
from split_songs import Match, Name  # noqa: E402

# Variáveis de ambiente que o programa lê e que não podem vazar para os testes.
ENV_KEYS = ["ACOUSTID_KEY", "ACRCLOUD_HOST", "ACRCLOUD_ACCESS_KEY", "ACRCLOUD_ACCESS_SECRET",
            "ACRCLOUD_INTERVAL", "ACRCLOUD_DAILY_LIMIT", "INPUT_DIR", "TEMP_DIR",
            "OUTPUT_DIR", "COOKIES_FROM_BROWSER"]


def quiet():
    """Descarta o que o programa imprime durante o teste."""
    return contextlib.redirect_stdout(io.StringIO())


def clean_env():
    env = mock.patch.dict(os.environ)
    env.start()
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    return env


class TempDirTest(unittest.TestCase):
    """Cria uma pasta temporária e aponta Input/Temp/Output do programa para ela."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.input = self.tmp / "Input"
        self.temp = self.tmp / "Temp"
        self.output = self.tmp / "Output"
        for d in (self.input, self.temp, self.output):
            d.mkdir()
        for name, value in (("INPUT_DIR", self.input), ("TEMP_DIR", self.temp),
                            ("OUTPUT_DIR", self.output),
                            ("ACR_USAGE_FILE", self.tmp / ".acrcloud_uso.json")):
            patcher = mock.patch.object(ss, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def touch(path, content=b"mp3"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


# --------------------------------------------------------------------------
# Funções puras
# --------------------------------------------------------------------------

class FmtTimeTest(unittest.TestCase):
    def test_formats_hours_minutes_seconds(self):
        self.assertEqual(ss.fmt_time(0), "00:00:00")
        self.assertEqual(ss.fmt_time(59.9), "00:00:59")
        self.assertEqual(ss.fmt_time(3661.9), "01:01:01")

    def test_negative_becomes_zero(self):
        self.assertEqual(ss.fmt_time(-5), "00:00:00")


class ParseTimeTest(unittest.TestCase):
    def test_valid_formats(self):
        self.assertEqual(ss.parse_time("0;03;45"), 225)
        self.assertEqual(ss.parse_time("1;00;00"), 3600)
        self.assertEqual(ss.parse_time("3:45.5"), 225.5)
        self.assertEqual(ss.parse_time(" 3 ; 45 "), 225)
        self.assertEqual(ss.parse_time("45"), 45)
        self.assertEqual(ss.parse_time("1,5"), 1.5)
        self.assertEqual(ss.parse_time("120"), 120)  # só segundos pode passar de 60

    def test_invalid_formats(self):
        for text in ("", "abc", "1;60", "1;2;60", "-1", "0;-1", "1;2;3;4", "1;;2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ss.parse_time(text)


class ReadTimesTest(TempDirTest):
    def test_ignores_comments_and_blank_lines(self):
        path = self.tmp / "t.txt"
        path.write_text("# cortes\n0;03;00\n\n0;06;30  # segunda\n", encoding="utf-8-sig")
        self.assertEqual(ss.read_times(path), [(2, 180.0), (4, 390.0)])

    def test_error_names_the_line(self):
        path = self.tmp / "t.txt"
        path.write_text("0;03;00\nerrado\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "linha 2"):
            ss.read_times(path)


class SilenceTest(unittest.TestCase):
    def test_choose_cuts_prefers_long_silences_and_respects_min_song(self):
        silences = [(100, 102, 2), (200, 201, 1), (250, 253, 3), (10, 11, 1)]
        cuts = ss.choose_cuts(silences, total=400, min_song=90)
        # (200, 201) fica a menos de 90 s do corte em 251.5; (10, 11) perto do início
        self.assertEqual(cuts, [(101, 100, 102), (251.5, 250, 253)])

    def test_choose_cuts_ignores_silence_near_the_end(self):
        self.assertEqual(ss.choose_cuts([(350, 352, 2)], total=400, min_song=90), [])

    def test_choose_cuts_without_silences(self):
        self.assertEqual(ss.choose_cuts([], total=400, min_song=90), [])

    def test_build_tracks_keeps_margin_inside_the_silence(self):
        cuts = [(101, 100, 102), (251.5, 250, 253)]
        self.assertEqual(ss.build_tracks(cuts, 400, margin=0.5),
                         [(0.0, 100.5), (101.5, 250.5), (252.5, 400)])

    def test_build_tracks_margin_never_passes_the_middle(self):
        self.assertEqual(ss.build_tracks([(101, 100, 102)], 200, margin=5),
                         [(0.0, 101), (101, 200)])

    def test_build_tracks_without_cuts(self):
        self.assertEqual(ss.build_tracks([], 300, margin=1.5), [(0.0, 300)])

    def test_detect_silences_parses_ffmpeg_output(self):
        stderr = ("[silencedetect] silence_start: 10\n"
                  "[silencedetect] silence_end: 12.5 | silence_duration: 2.5\n"
                  "[silencedetect] silence_end: 30 | silence_duration: 1\n")
        proc = subprocess.CompletedProcess([], 0, "", stderr)
        with mock.patch.object(ss, "tool", return_value="ffmpeg"), \
                mock.patch.object(ss.subprocess, "run", return_value=proc) as run:
            self.assertEqual(ss.detect_silences(Path("a.mp3"), -35, 0.5),
                             [(10.0, 12.5, 2.5), (29.0, 30.0, 1.0)])
        self.assertIn("silencedetect=noise=-35dB:d=0.5", run.call_args[0][0])


class TextTest(unittest.TestCase):
    def test_clean_text_removes_control_and_bidi_characters(self):
        self.assertEqual(ss.clean_text("a\tb\r\nc"), "a b c")
        self.assertEqual(ss.clean_text("\x1b[31mred\x00"), "[31mred")
        self.assertEqual(ss.clean_text("song‮3pm.exe"), "song3pm.exe")
        self.assertEqual(ss.clean_text("  muitos    espaços "), "muitos espaços")
        self.assertEqual(ss.clean_text(None), "")
        self.assertEqual(ss.clean_text(42), "42")

    def test_fmt_time_precise(self):
        self.assertEqual(ss.fmt_time_precise(0), "00:00:00.00")
        self.assertEqual(ss.fmt_time_precise(100.5), "00:01:40.50")
        self.assertEqual(ss.fmt_time_precise(3599.999), "01:00:00.00")
        self.assertEqual(ss.fmt_time_precise(-1), "00:00:00.00")
        for value in (0.0, 100.5, 3725.25, 59.99):
            self.assertEqual(ss.parse_time(ss.fmt_time_precise(value)), value)

    def test_label_roundtrip(self):
        for name in (Name("Queen", "Bohemian Rhapsody", "acoustid", False),
                     Name("A", "Song - Live [Mono]", "acrcloud", True),
                     Name("A", "B", "", True), Name("A", "B"), None):
            with self.subTest(name=name):
                self.assertEqual(ss.parse_label(ss.log_label(name)), name)
        self.assertEqual(ss.log_label(Name("A", "B", "acoustid", True)),
                         "A - B [acoustid, conferir]")

    def test_parse_label(self):
        self.assertIsNone(ss.parse_label(""))
        self.assertIsNone(ss.parse_label("(não identificada)"))
        self.assertEqual(ss.parse_label("  The Who -  My Generation  "),
                         Name("The Who", "My Generation"))
        self.assertEqual(ss.parse_label("A - B [Conferir]"), Name("A", "B", "", True))
        for bad in ("Só o título", "A - ", " - B"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ss.parse_label(bad)

    def test_clean_filename_removes_emojis_and_symbols(self):
        self.assertEqual(ss.clean_filename("🔥 Rock  Hits ♫ "), "Rock Hits")
        self.assertEqual(ss.clean_filename("❤️ Love"), "Love")
        self.assertEqual(ss.clean_filename("-- abc --"), "abc")
        self.assertEqual(ss.clean_filename("🔥"), "audio")
        self.assertEqual(ss.clean_filename("Clásicos ｜ Rock"), "Clásicos ｜ Rock")

    def test_safe_filename(self):
        self.assertEqual(ss.safe_filename('AC/DC: "Back" <in> Black?*|\\'), "ACDC Back in Black")
        self.assertEqual(ss.safe_filename("...nome..."), "nome")
        self.assertEqual(ss.safe_filename("a‮b\nc"), "ab c")
        self.assertEqual(len(ss.safe_filename("a" * 300)), ss.MAX_NAME)

    def test_source_name_strips_folders(self):
        self.assertEqual(ss.source_name("album.mp3"), "album.mp3")
        self.assertEqual(ss.source_name("/etc/passwd"), "passwd")
        self.assertEqual(ss.source_name("..\\..\\x.mp3"), "x.mp3")
        self.assertEqual(ss.source_name("../../x.mp3\n"), "x.mp3")
        for bad in ("", "..", ".", "pasta/"):
            with self.subTest(bad=bad):
                self.assertEqual(ss.source_name(bad), "")

    def test_join_artists(self):
        credits = [{"name": "Simon", "joinphrase": " & "}, {"name": "Garfunkel"}]
        self.assertEqual(ss.join_artists(credits), "Simon & Garfunkel")
        self.assertEqual(ss.join_artists([]), "")

    def test_artist_key(self):
        self.assertEqual(ss.artist_key("03 - The Doors - Light My Fire.mp3"), "doors")
        self.assertEqual(ss.artist_key(Path("x/1 - Guns N’ Roses - Song.mp3")), "guns n' roses")
        self.assertEqual(ss.artist_key("random.mp3"), "random")


class InputFileTest(TempDirTest):
    def test_accepts_only_files_directly_inside_input(self):
        self.assertEqual(ss.input_file("album.mp3"), self.input / "album.mp3")
        for bad in ("", ".", "..", "../album.mp3", "sub/album.mp3"):
            with self.subTest(bad=bad):
                self.assertIsNone(ss.input_file(bad))


class UniquePathTest(TempDirTest):
    def test_adds_counter_when_file_exists(self):
        path = self.tmp / "a.mp3"
        self.assertEqual(ss.unique_path(path), path)
        self.touch(path)
        self.assertEqual(ss.unique_path(path), self.tmp / "a (2).mp3")
        self.touch(self.tmp / "a (2).mp3")
        self.assertEqual(ss.unique_path(path), self.tmp / "a (3).mp3")


# --------------------------------------------------------------------------
# Votação e identificação
# --------------------------------------------------------------------------

class VotingTest(unittest.TestCase):
    def test_vote_key_ignores_case_and_versions(self):
        self.assertEqual(ss.vote_key(Match("The Beatles", "Let It Be (Remastered 2009)", 1, "")),
                         ("the beatles", "let it be"))
        self.assertEqual(ss.vote_key(Match("A", "Song - 2011 Remaster", 1, "")), ("a", "song"))
        self.assertEqual(ss.vote_key(Match("A", "Song - Live at Wembley", 1, "")), ("a", "song"))
        self.assertEqual(ss.vote_key(Match("A", "Song [Mono]", 1, "")), ("a", "song"))
        self.assertEqual(ss.vote_key(Match("A", "Song - Part 2", 1, "")), ("a", "song - part 2"))

    def test_elect_majority_wins(self):
        matches = [Match("A", "X", 0.6, "acoustid"), Match("B", "Y", 0.99, "acoustid"),
                   Match("a", "X (Remastered)", 0.9, "acrcloud")]
        name, votes = ss.elect(matches)
        self.assertEqual(votes, 2)
        self.assertEqual(name, Name("a", "X (Remastered)", "acrcloud", False))

    def test_elect_marks_tie_and_low_score_for_review(self):
        name, _ = ss.elect([Match("A", "X", 0.9, "s"), Match("B", "Y", 0.8, "s")])
        self.assertEqual((name.artist, name.review), ("A", True))
        name, _ = ss.elect([Match("A", "X", 0.4, "s")])
        self.assertTrue(name.review)

    def test_elect_cleans_external_text(self):
        name, _ = ss.elect([Match("A‮", "X\n\x1b", 0.9, "s")])
        self.assertEqual((name.artist, name.title), ("A", "X"))

    def test_elect_without_matches(self):
        self.assertIsNone(ss.elect([]))

    def test_poll_stops_when_result_is_decided(self):
        query = mock.Mock(return_value=Match("A", "X", 0.9, "s"))
        errors = []
        self.assertEqual(len(ss.poll([1, 2, 3, 4], query, errors)), 3)
        self.assertEqual(query.call_count, 3)  # o 4º trecho não muda o resultado
        self.assertEqual(errors, [])

    def test_poll_collects_errors_and_continues(self):
        query = mock.Mock(side_effect=[RuntimeError("falhou"), None, Match("A", "X", 0.9, "s")])
        errors = []
        self.assertEqual(ss.poll([1, 2, 3], query, errors), [Match("A", "X", 0.9, "s")])
        self.assertEqual(len(errors), 1)

    def test_poll_stops_at_acr_limit(self):
        query = mock.Mock(side_effect=ss.AcrLimitError("limite"))
        errors = []
        self.assertEqual(ss.poll([1, 2, 3], query, errors), [])
        self.assertEqual(query.call_count, 1)
        self.assertIsInstance(errors[0], ss.AcrLimitError)

    def test_acoustid_offsets(self):
        self.assertEqual(ss.acoustid_offsets(1), [0.0])
        self.assertEqual(ss.acoustid_offsets(4), [0.0, -5.0, 5.0, -10.0])
        self.assertEqual(ss.acoustid_offsets(5), [0.0, -5.0, 5.0, -10.0, 10.0])


class IdentifyTrackTest(unittest.TestCase):
    def test_uses_acoustid_and_skips_acr_when_confident(self):
        acr = mock.Mock(available=True)
        with mock.patch.object(ss, "acoustid_lookup",
                               return_value=Match("Queen", "Bohemian Rhapsody", 0.9, "acoustid")
                               ) as lookup:
            name, votes, answers, errors = ss.identify_track("src.mp3", 100, 300, 3, "key", acr, {})
        self.assertEqual(name, Name("Queen", "Bohemian Rhapsody", "acoustid", False))
        self.assertEqual((votes, answers, errors), (2, 2, []))
        self.assertEqual([c.args[1] for c in lookup.call_args_list], [100, 95])
        acr.identify.assert_not_called()

    def test_falls_back_to_acrcloud(self):
        acr = mock.Mock(available=True)
        acr.identify.return_value = Match("Queen", "Radio Ga Ga", 0.95, "acrcloud")
        name, votes, answers, _ = ss.identify_track("src.mp3", 100, 300, 3, None, acr, {})
        self.assertEqual(name.source, "acrcloud")
        # trechos de 12 s espalhados pela música
        self.assertEqual([c.args[1] for c in acr.identify.call_args_list], [144.0, 194.0])
        self.assertEqual(acr.identify.call_args.args[2], ss.ACR_SAMPLE_SECONDS)

    def test_nothing_found(self):
        with mock.patch.object(ss, "acoustid_lookup", return_value=None):
            self.assertEqual(ss.identify_track("s", 0, 200, 2, "key", None, {}),
                             (None, 0, 0, []))


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class AcoustIdTest(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(ss, "fingerprint", return_value="FP"),
                        mock.patch.object(ss.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lookup(self, data, duration=105):
        with mock.patch.object(ss.urllib.request, "urlopen",
                               return_value=FakeResponse(data)) as urlopen:
            result = ss.acoustid_lookup("src.mp3", 0, 120, duration, "KEY", {})
        return result, urlopen

    def test_picks_best_score_then_closest_duration(self):
        data = {"status": "ok", "results": [
            {"score": 0.2, "recordings": [{"title": "Ruim", "artists": [{"name": "Z"}]}]},
            {"score": 0.9, "recordings": [
                {"title": "T", "artists": [{"name": "A"}], "duration": 200},
                {"title": "T2", "artists": [{"name": "B"}], "duration": 100},
            ]},
        ]}
        result, urlopen = self.lookup(data)
        self.assertEqual(result, Match("B", "T2", 0.9, "acoustid"))
        url = urlopen.call_args.args[0]
        body = urlopen.call_args.kwargs["data"].decode()
        self.assertEqual(url, ss.ACOUSTID_URL)
        self.assertIn("client=KEY", body)
        self.assertIn("fingerprint=FP", body)
        self.assertIn("duration=105", body)

    def test_error_status_raises(self):
        with self.assertRaisesRegex(RuntimeError, "chave inválida"):
            self.lookup({"status": "error", "error": {"message": "chave inválida"}})

    def test_orphan_recording_uses_musicbrainz(self):
        data = {"status": "ok", "results": [{"score": 0.8, "recordings": [{"id": "rec-1"}]}]}
        with mock.patch.object(ss, "musicbrainz_recording", return_value=("A", "T")) as mb:
            result, _ = self.lookup(data)
        self.assertEqual(result, Match("A", "T", 0.8, "acoustid"))
        self.assertEqual(mb.call_args.args[0], "rec-1")

    def test_no_results(self):
        self.assertIsNone(self.lookup({"status": "ok", "results": []})[0])


class MusicBrainzTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ss.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_result_is_cached(self):
        data = {"title": "T", "artist-credit": [{"name": "A", "joinphrase": " feat. "},
                                                {"name": "B"}]}
        cache = {}
        with mock.patch.object(ss.urllib.request, "urlopen",
                               return_value=FakeResponse(data)) as urlopen:
            self.assertEqual(ss.musicbrainz_recording("id", cache), ("A feat. B", "T"))
            self.assertEqual(ss.musicbrainz_recording("id", cache), ("A feat. B", "T"))
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(urlopen.call_args.args[0].get_header("User-agent"))

    def test_network_error_returns_none(self):
        with mock.patch.object(ss.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(ss.musicbrainz_recording("id", {}))


class AcrCloudTest(TempDirTest):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(ss, "extract_sample", return_value=b"sample"),
                        mock.patch.object(ss.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, responses, daily_limit=100):
        acr = ss.AcrCloud("https://identify.example.com/", "key", "secret", 0, daily_limit)
        acr._post = mock.Mock(side_effect=responses)
        return acr

    @staticmethod
    def found(*music):
        return {"status": {"code": 0}, "metadata": {"music": list(music)}}

    def test_host_is_normalized(self):
        self.assertEqual(self.client([]).host, "identify.example.com")

    def test_best_match(self):
        acr = self.client([self.found(
            {"title": "Baixo", "artists": [{"name": "X"}], "score": 20},
            {"title": "T", "artists": [{"name": "A"}, {"name": "B"}], "score": 90,
             "duration_ms": 200000},
        )])
        self.assertEqual(acr.identify("src", 0, 12, 200), Match("A, B", "T", 0.9, "acrcloud"))
        usage = json.loads(ss.ACR_USAGE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(usage["envios"], 1)

    def test_no_music_found(self):
        self.assertIsNone(self.client([{"status": {"code": 1001}}]).identify("s", 0, 12, 200))

    def test_retries_once_when_rate_limited(self):
        acr = self.client([{"status": {"code": 3015}},
                           self.found({"title": "T", "artists": [{"name": "A"}], "score": 80})])
        self.assertEqual(acr.identify("s", 0, 12, 200).title, "T")
        self.assertEqual(acr._post.call_count, 2)

    def test_account_limit_disables_client(self):
        acr = self.client([{"status": {"code": 3003}}])
        with self.assertRaises(ss.AcrLimitError):
            acr.identify("s", 0, 12, 200)
        self.assertFalse(acr.available)
        with self.assertRaises(ss.AcrLimitError):  # não envia mais nada
            acr.identify("s", 0, 12, 200)
        self.assertEqual(acr._post.call_count, 1)

    def test_daily_limit(self):
        acr = self.client([{"status": {"code": 1001}}] * 3, daily_limit=1)
        acr.identify("s", 0, 12, 200)
        with self.assertRaisesRegex(ss.AcrLimitError, "limite diário"):
            acr.identify("s", 0, 12, 200)
        self.assertEqual(acr._post.call_count, 1)

    def test_other_error_raises(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.client([{"status": {"code": 2004, "msg": "boom"}}]).identify("s", 0, 12, 200)

    def test_post_builds_signed_multipart_request(self):
        acr = ss.AcrCloud("identify.example.com", "key", "secret", 0, 100)
        with mock.patch.object(ss.time, "time", return_value=1000), \
                mock.patch.object(ss.urllib.request, "urlopen",
                                  return_value=FakeResponse({"status": {"code": 0}})) as urlopen:
            acr._post(b"AUDIO")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://identify.example.com/v1/identify")
        body = req.data
        self.assertIn(b"AUDIO", body)
        self.assertIn(b'name="access_key"\r\n\r\nkey\r\n', body)
        self.assertIn(b'name="timestamp"\r\n\r\n1000\r\n', body)
        to_sign = "POST\n/v1/identify\nkey\naudio\n1\n1000"
        signature = ss.base64.b64encode(ss.hmac.new(b"secret", to_sign.encode(),
                                                    ss.hashlib.sha1).digest())
        self.assertIn(signature, body)


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

class BaseDirTest(unittest.TestCase):
    """--base-dir: a pasta de dados (.env, Input, Temp, Output) vem da linha de comando."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.data = self.tmp / "dados"
        self.data.mkdir()
        self.addCleanup(clean_env().stop)
        # o main() troca as variáveis globais; o patch as restaura depois
        for name in ("BASE_DIR", "INPUT_DIR", "TEMP_DIR", "OUTPUT_DIR", "ENV_FILE",
                     "ACR_USAGE_FILE"):
            patcher = mock.patch.object(ss, name, getattr(ss, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *args):
        with mock.patch.object(sys, "argv", ["split_songs.py", *args]), quiet(), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                ss.main()
            except SystemExit as exc:
                return exc.code
        return None

    def test_default_is_the_script_folder(self):
        self.assertEqual(ss.BASE_DIR, ROOT)

    def test_set_base_dir(self):
        ss.set_base_dir(self.data)
        self.assertEqual((ss.BASE_DIR, ss.INPUT_DIR, ss.TEMP_DIR, ss.OUTPUT_DIR, ss.ENV_FILE,
                          ss.ACR_USAGE_FILE),
                         (self.data, self.data / "Input", self.data / "Temp",
                          self.data / "Output", self.data / ".env",
                          self.data / ".acrcloud_uso.json"))

    def test_uses_output_inside_base_dir(self):
        TempDirTest.touch(self.data / "Output" / "album" / "01 - A - X.mp3")
        dest = self.tmp / "dest"
        self.assertIsNone(self.run_main("--base-dir", str(self.data), "--copy", str(dest)))
        self.assertTrue((dest / "01 - album - 01 - A - X.mp3").is_file())

    def test_reads_env_from_base_dir(self):
        # output-dir relativo no .env parte da pasta de dados
        (self.data / ".env").write_text("output-dir=Saida\n", encoding="utf-8")
        if os.name == "posix":
            (self.data / ".env").chmod(0o600)
        TempDirTest.touch(self.data / "Saida" / "01 - A - X.mp3")
        dest = self.tmp / "dest"
        self.assertIsNone(self.run_main(f"--base-dir={self.data}", "--copy", str(dest)))
        self.assertTrue((dest / "01 - 01 - A - X.mp3").is_file())

    def test_relative_base_dir(self):
        TempDirTest.touch(self.data / "Output" / "01 - A - X.mp3")
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            self.assertIsNone(self.run_main("--base-dir", "dados", "--copy", "dest"))
        finally:
            os.chdir(cwd)
        self.assertEqual(ss.BASE_DIR, self.data)
        self.assertTrue((self.tmp / "dest" / "01 - 01 - A - X.mp3").is_file())

    def test_missing_base_dir(self):
        code = self.run_main("--base-dir", str(self.tmp / "nao_existe"), "--version")
        self.assertIn("pasta não encontrada", str(code))


class ConfigTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.addCleanup(clean_env().stop)

    def test_load_env(self):
        path = self.tmp / ".env"
        path.write_text("# comentário\n\nACOUSTID_KEY = \"abc\"\nInput_Dir='~/x'\nsem igual\n"
                        "URL=a=b\n", encoding="utf-8-sig")
        if os.name == "posix":
            path.chmod(0o600)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            config = ss.load_env(path)
        self.assertEqual(config, {"acoustid-key": "abc", "input-dir": "~/x", "url": "a=b"})
        self.assertEqual(err.getvalue(), "")

    def test_load_env_missing_file(self):
        self.assertEqual(ss.load_env(self.tmp / "nao_existe"), {})

    @unittest.skipUnless(os.name == "posix", "permissões só no Linux/macOS")
    def test_warns_when_env_is_readable_by_others(self):
        path = self.tmp / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        path.chmod(0o644)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ss.load_env(path)
        self.assertIn("chmod 600", err.getvalue())

    def test_environment_has_priority(self):
        config = {"acoustid-key": "do-env-file"}
        self.assertEqual(ss.setting(config, "acoustid-key"), "do-env-file")
        self.assertEqual(ss.setting({}, "acoustid-key", "padrao"), "padrao")
        os.environ["ACOUSTID_KEY"] = "do-ambiente"
        self.assertEqual(ss.setting(config, "acoustid-key"), "do-ambiente")

    def test_config_dir(self):
        default = Path("/padrao")
        self.assertEqual(ss.config_dir({}, "input-dir", default), default)
        self.assertEqual(ss.config_dir({"input-dir": "Musicas"}, "input-dir", default),
                         (ss.BASE_DIR / "Musicas").resolve())
        self.assertEqual(ss.config_dir({"input-dir": str(self.tmp)}, "input-dir", default),
                         self.tmp)

    def test_build_acrcloud(self):
        self.assertIsNone(ss.build_acrcloud({"acrcloud-host": "h"}))
        acr = ss.build_acrcloud({"acrcloud-host": "h", "acrcloud-access-key": "k",
                                 "acrcloud-access-secret": "s", "acrcloud-interval": "2",
                                 "acrcloud-daily-limit": "10"})
        self.assertEqual((acr.host, acr.interval, acr.daily_limit), ("h", 2.0, 10))


class WhichTrustedTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.addCleanup(clean_env().stop)
        self.name = "fakeprog"
        exe = self.tmp / "bin" / (self.name + (".exe" if os.name == "nt" else ""))
        self.touch(exe)
        exe.chmod(0o755)
        self.exe = exe

    def test_finds_program_in_absolute_path_entries(self):
        os.environ["PATH"] = os.pathsep.join(["", str(self.tmp / "bin")])
        self.assertEqual(ss.which_trusted(self.name), str(self.exe))

    def test_ignores_relative_path_entries(self):
        os.environ["PATH"] = os.pathsep.join([".", "bin"])
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            self.assertIsNone(ss.which_trusted(self.name))
        finally:
            os.chdir(cwd)

    def test_missing_tool_exits(self):
        os.environ["PATH"] = ""
        with mock.patch.dict(ss._TOOLS, clear=True), self.assertRaises(SystemExit):
            ss.tool("programa-que-nao-existe")


# --------------------------------------------------------------------------
# Arquivos: registro, exportação, publicação e cópia
# --------------------------------------------------------------------------

class LogTest(TempDirTest):
    def test_write_and_read_roundtrip(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        tracks = [(0.0, 100.5), (101.5, 250.0)]
        names = [Name("=Evil", "Song", "acoustid", True), None]
        lines = ss.write_log(out_dir, "album.mp3", tracks, names)
        self.assertEqual(lines, [
            "01  00:00:00.00 - 00:01:40.50  (00:01:40)  =Evil - Song [acoustid, conferir]",
            "02  00:01:41.50 - 00:04:10.00  (00:02:28)  (não identificada)"])
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["musicas.txt"])
        text = (out_dir / "musicas.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("album.mp3\n# "))
        self.assertTrue(text.endswith(lines[-1] + "\n"))
        self.assertEqual(ss.read_log(out_dir), ("album.mp3", tracks, names))

    def test_read_log_accepts_manual_edits(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        (out_dir / "musicas.txt").write_text(
            "\ufeffalbum.mp3\n"
            "# comentário\n"
            "\n"
            "01  00:00:00 - 00:03:10  (00:03:10)  The Who - My Generation\n"
            "2   3:10 - 6:00.5   Queen - Radio Ga Ga [conferir]\n"
            "6:00,5-0;09;00\n",
            encoding="utf-8")
        self.assertEqual(ss.read_log(out_dir), (
            "album.mp3",
            [(0.0, 190.0), (190.0, 360.5), (360.5, 540.0)],
            [Name("The Who", "My Generation"), Name("Queen", "Radio Ga Ga", "", True), None]))

    def test_read_log_errors_name_the_line(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        for line, message in (("01  texto qualquer", "linha 3: use"),
                              ("01  00:03:00 - 00:02:00", "linha 3: o fim"),
                              ("01  00:00:00 - 00:75:00", "linha 3: tempo inválido"),
                              ("01  00:00:00 - 00:01:00  Só título", "linha 3: nome")):
            (out_dir / "musicas.txt").write_text(f"album.mp3\n# x\n{line}\n", encoding="utf-8")
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, message):
                ss.read_log(out_dir)

    def test_read_log_without_file(self):
        self.assertIsNone(ss.read_log(self.temp))

    def test_read_log_rejects_paths_in_source_name(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        ss.write_log(out_dir, "../../segredo.mp3", [(0.0, 10.0)], [None])
        self.assertEqual(ss.read_log(out_dir)[0], "segredo.mp3")
        (out_dir / "musicas.txt").write_text("\n01  00:00:00 - 00:00:10\n", encoding="utf-8")
        self.assertEqual(ss.read_log(out_dir)[0], "album.mp3")

    def test_process_writes_log(self):
        args = mock.Mock(noise=-35, min_silence=0.5, min_song=90, margin=0.5)
        path = self.touch(self.input / "album.mp3")
        with mock.patch.object(ss, "get_duration", return_value=400.0), \
                mock.patch.object(ss, "detect_silences", return_value=[(100, 102, 2)]), quiet():
            ss.process(path, args)
        src, tracks, names = ss.read_log(self.temp / "album")
        self.assertEqual((src, tracks, names), ("album.mp3", [(0.0, 100.5), (101.5, 400.0)],
                                                [None, None]))


class ExportTest(TempDirTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ss, "export_track")
        self.export_track = patcher.start()
        self.addCleanup(patcher.stop)

    def test_export_tracks_replaces_only_program_files(self):
        out_dir = self.temp / "album"
        self.touch(out_dir / "05 - Antiga.mp3")
        self.touch(out_dir / "minha musica.mp3")
        tracks = [(0.0, 100.0), (100.0, 200.0)]
        names = [Name("AC/DC", "Back: in Black"), None]
        with quiet():
            ss.export_tracks(Path("src.mp3"), out_dir, tracks, names)
        self.assertFalse((out_dir / "05 - Antiga.mp3").exists())
        self.assertTrue((out_dir / "minha musica.mp3").exists())
        self.assertEqual([c.args for c in self.export_track.call_args_list], [
            (Path("src.mp3"), out_dir / "01 - ACDC - Back in Black.mp3", 0.0, 100.0, 1,
             "AC/DC", "Back: in Black"),
            (Path("src.mp3"), out_dir / "02 - Faixa 02.mp3", 100.0, 200.0, 2, None, None),
        ])

    def test_split_tracks(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        ss.write_log(out_dir, "album.mp3", [(0.0, 100.0)], [None])
        with quiet():
            self.assertFalse(ss.split_tracks(out_dir))  # o MP3 de origem não existe
            self.touch(self.input / "album.mp3")
            self.assertTrue(ss.split_tracks(out_dir))
            self.assertFalse(ss.split_tracks(self.temp / "sem_registro"))
        self.assertEqual(self.export_track.call_count, 1)

    def test_split_tracks_with_source_outside_input(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        ss.write_log(out_dir, "album.mp3", [(0.0, 100.0)], [None])
        src = self.touch(self.tmp / "outra pasta" / "album.mp3")
        with quiet() as out:
            self.assertFalse(ss.split_tracks(out_dir))  # sozinho, só procura em Input
            self.assertIn("passe o arquivo na linha de comando", out.getvalue())
            self.assertTrue(ss.split_tracks(out_dir, src))
        self.assertEqual(self.export_track.call_args.args[0], src)

    def test_rename_tracks_with_source_outside_input(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        ss.write_log(out_dir, "album.mp3", [(10.0, 100.0)], [None])
        self.touch(out_dir / "01 - Faixa 01.mp3")
        src = self.touch(self.tmp / "outra pasta" / "album.mp3")
        with mock.patch.object(ss, "get_duration", return_value=90.0), \
                mock.patch.object(ss, "identify_track", return_value=(None, 0, 0, [])) as identify, \
                quiet():
            ss.rename_tracks(out_dir, "key", None, 4, {}, src)
        self.assertEqual(identify.call_args.args[:3], (src, 10.0, 100.0))

    def test_split_tracks_skips_invalid_log(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        self.touch(self.input / "album.mp3")
        (out_dir / "musicas.txt").write_text("album.mp3\n01  errado\n", encoding="utf-8")
        with quiet() as out:
            self.assertFalse(ss.split_tracks(out_dir))
        self.assertIn("musicas.txt, linha 2", out.getvalue())
        self.export_track.assert_not_called()

    def test_manual_split(self):
        src = self.touch(self.input / "show.mp3")
        times = self.input / "show.txt"
        times.write_text("0;03;00\n0;06;00\n", encoding="utf-8")
        with mock.patch.object(ss, "get_duration", return_value=600.0), quiet():
            out_dir = ss.manual_split(src, times)
        self.assertEqual(out_dir, self.temp / "show")
        self.assertEqual(ss.read_log(out_dir)[1], [(0.0, 180.0), (180.0, 360.0), (360.0, 600.0)])
        self.assertEqual(self.export_track.call_count, 3)

    def test_manual_split_ignores_tiny_rest(self):
        src = self.touch(self.input / "show.mp3")
        times = self.input / "show.txt"
        times.write_text("0;09;59.5\n", encoding="utf-8")
        with mock.patch.object(ss, "get_duration", return_value=600.0), quiet():
            out_dir = ss.manual_split(src, times)
        self.assertEqual(ss.read_log(out_dir)[1], [(0.0, 599.5)])

    def test_manual_split_errors(self):
        src = self.touch(self.input / "show.mp3")
        times = self.input / "show.txt"
        for content in ("0;03;00\n0;02;00\n", "0;20;00\n", "# vazio\n", "abc\n"):
            times.write_text(content, encoding="utf-8")
            with self.subTest(content=content), quiet(), \
                    mock.patch.object(ss, "get_duration", return_value=600.0):
                self.assertIsNone(ss.manual_split(src, times))
        with quiet():
            self.assertIsNone(ss.manual_split(src, self.input / "nao_existe.txt"))
            self.assertIsNone(ss.manual_split(self.input / "nao_existe.mp3", times))
        self.export_track.assert_not_called()

    def test_rename_tracks(self):
        out_dir = self.temp / "album"
        out_dir.mkdir()
        ss.write_log(out_dir, "album.mp3", [(0.0, 100.0), (100.0, 200.0)], [None, None])
        self.touch(out_dir / "01 - Faixa 01.mp3")
        self.touch(out_dir / "02 - Faixa 02.mp3")
        self.touch(self.input / "album.mp3")
        queen = Name("Queen", "Bohemian Rhapsody", "acoustid", False)
        with mock.patch.object(ss, "get_duration", return_value=100.0), \
                mock.patch.object(ss, "identify_track",
                                  side_effect=[(queen, 2, 2, []), (None, 0, 0, [])]) as identify, \
                quiet():
            self.assertTrue(ss.rename_tracks(out_dir, "key", None, 4, {}))
        # os trechos vêm do MP3 original, nos tempos do musicas.txt
        self.assertEqual(identify.call_args_list[0].args[:4], (self.input / "album.mp3", 0.0,
                                                               100.0, 4))
        self.assertEqual(self.export_track.call_args.args[1:],
                         (out_dir / "01 - Queen - Bohemian Rhapsody.mp3", 0.0, 100.0, 1,
                          "Queen", "Bohemian Rhapsody"))
        self.assertFalse((out_dir / "01 - Faixa 01.mp3").exists())
        self.assertTrue((out_dir / "02 - Faixa 02.mp3").exists())
        self.assertEqual(ss.read_log(out_dir)[2], [queen, None])


class PublishTest(TempDirTest):
    def test_replaces_previous_copies(self):
        src = self.temp / "album"
        self.touch(src / "01 - A.mp3")
        self.touch(src / "02 - B.mp3")
        with quiet():
            ss.publish_tracks(src)
        dest = self.output / "album"
        self.assertEqual(sorted(p.name for p in dest.glob("*.mp3")), ["01 - A.mp3", "02 - B.mp3"])
        (src / "02 - B.mp3").unlink()
        self.touch(src / "03 - C.mp3")
        with quiet():
            ss.publish_tracks(src)
        self.assertEqual(sorted(p.name for p in dest.glob("*.mp3")), ["01 - A.mp3", "03 - C.mp3"])
        self.assertEqual(json.loads((dest / ss.MANIFEST).read_text(encoding="utf-8")),
                         ["01 - A.mp3", "03 - C.mp3"])

    def test_keeps_foreign_files_without_manifest(self):
        src = self.temp / "album"
        self.touch(src / "01 - A.mp3")
        foreign = self.touch(self.output / "album" / "minha.mp3")
        old_track = self.touch(self.output / "album" / "09 - Velha.mp3")
        with quiet() as out:
            ss.publish_tracks(src)
        self.assertTrue(foreign.exists())
        self.assertTrue(old_track.exists())
        self.assertIn("nada foi apagado", out.getvalue())

    def test_cleans_old_tracks_without_manifest(self):
        src = self.temp / "album"
        self.touch(src / "01 - A.mp3")
        old_track = self.touch(self.output / "album" / "09 - Velha.mp3")
        with quiet():
            ss.publish_tracks(src)
        self.assertFalse(old_track.exists())

    def test_manifest_cannot_delete_outside_folder(self):
        src = self.temp / "album"
        self.touch(src / "01 - A.mp3")
        outside = self.touch(self.tmp / "fora.mp3")
        dest = self.output / "album"
        dest.mkdir()
        (dest / ss.MANIFEST).write_text(json.dumps(["../../fora.mp3", str(outside)]),
                                        encoding="utf-8")
        with quiet():
            ss.publish_tracks(src)
        self.assertTrue(outside.exists())

    def test_empty_folder_does_nothing(self):
        (self.temp / "vazio").mkdir()
        ss.publish_tracks(self.temp / "vazio")
        self.assertFalse((self.output / "vazio").exists())


class CopyShuffledTest(TempDirTest):
    def test_shuffles_without_repeating_artist_and_skips_duplicates(self):
        self.touch(self.output / "A" / "01 - Doors - X.mp3")
        self.touch(self.output / "A" / "02 - Doors - Y.mp3")
        self.touch(self.output / "B" / "01 - Queen - Z.mp3")
        self.touch(self.output / "B" / "02 - The Doors - X (Remastered).mp3")  # repetida
        dest = self.output / "pendrive"  # dentro do Output: não pode ser copiado de novo
        for seed in range(5):
            random.seed(seed)
            with self.subTest(seed=seed), quiet():
                ss.copy_shuffled(dest)
                lines = (dest / "lista.txt").read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 3)
                self.assertEqual([ln[:2] for ln in lines], ["01", "02", "03"])
                artists = [ss.artist_key(ln.split(" - ", 2)[2]) for ln in lines]
                self.assertEqual(artists, ["doors", "queen", "doors"])
                for ln in lines:
                    self.assertTrue((dest / ln).is_file())
                    self.assertRegex(ln, r"^\d{2} - [AB] - \d{2} - ")

    def test_without_files_exits(self):
        with self.assertRaises(SystemExit):
            ss.copy_shuffled(self.tmp / "dest")


# --------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------

class CliTest(TempDirTest):
    """Base para rodar o main() com as pastas temporárias e sem o .env real."""

    def setUp(self):
        super().setUp()
        self.addCleanup(clean_env().stop)
        patcher = mock.patch.object(ss, "load_env", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *args):
        argv = ["split_songs.py", "--input", str(self.input), "--temp", str(self.temp),
                "--output", str(self.output), *args]
        with mock.patch.object(sys, "argv", argv), quiet() as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                ss.main()
            except SystemExit as exc:
                return exc.code, out.getvalue() + err.getvalue()
        return None, out.getvalue() + err.getvalue()


class MainTest(CliTest):
    def test_version(self):
        code, out = self.run_main("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"SplitSong {ss.__version__}")

    def test_invalid_options(self):
        for args, message in ((["--times", "x.txt"], "--times só funciona"),
                              (["--repeat", "0"], "--repeat precisa"),
                              (["--rename"], "--rename precisa da chave"),
                              (["--download", "file:///etc/passwd"], "só links http"),
                              (["--download=-oexec"], "só links http"),
                              ([], "Nenhum MP3 encontrado")):
            with self.subTest(args=args):
                code, _ = self.run_main(*args)
                self.assertIn(message, str(code))

    def test_wildcard_not_expanded(self):
        self.touch(self.input / "a.mp3")
        pattern = str(self.input / "*.mp3")  # entre aspas, o shell não expande
        code, _ = self.run_main(pattern, "--detect")
        self.assertIn("caracteres de pesquisa", str(code))
        self.assertIn(repr(pattern), str(code))
        self.assertFalse(any(self.temp.iterdir()))  # nada foi processado

    def test_check_files(self):
        real = self.touch(self.input / "Show [Live].mp3")  # colchetes de verdade no nome
        ss.check_files([real])
        for path, message in ((self.input / "Faixa ?.mp3", "caracteres de pesquisa"),
                              (self.input / "[ab].mp3", "caracteres de pesquisa"),
                              (self.input / "nao_existe.mp3", "arquivo não encontrado"),
                              (self.input, "é uma pasta")):
            with self.subTest(path=path), self.assertRaises(SystemExit) as ctx:
                ss.check_files([real, path])
            self.assertIn(message, str(ctx.exception.code))

    def test_check_files_lists_every_problem(self):
        with self.assertRaises(SystemExit) as ctx:
            ss.check_files([self.tmp / "x.mp3", self.tmp / "*.mp3"])
        self.assertEqual(str(ctx.exception.code).count("erro:"), 2)

    def test_manual_times_requires_single_file(self):
        self.touch(self.input / "a.mp3")
        self.touch(self.input / "b.mp3")
        code, _ = self.run_main("--manual", "--times", "x.txt")
        self.assertIn("um único MP3", str(code))

    def test_copy(self):
        self.touch(self.output / "album" / "01 - A - X.mp3")
        dest = self.tmp / "dest"
        code, _ = self.run_main("--copy", str(dest))
        self.assertIsNone(code)
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["01 - album - 01 - A - X.mp3", "lista.txt"])

    def test_detect_then_split(self):
        self.touch(self.input / "album.mp3")
        with mock.patch.object(ss, "get_duration", return_value=400.0), \
                mock.patch.object(ss, "detect_silences", return_value=[(199, 201, 2)]), \
                mock.patch.object(ss, "export_track",
                                  side_effect=lambda src, dest, *a: self.touch(dest)):
            self.assertEqual(self.run_main("--detect")[0], None)
            self.assertEqual(len(ss.read_log(self.temp / "album")[1]), 2)
            self.assertFalse((self.output / "album").exists())
            self.assertEqual(self.run_main("--split")[0], None)
        self.assertEqual(sorted(p.name for p in (self.output / "album").glob("*.mp3")),
                         ["01 - Faixa 01.mp3", "02 - Faixa 02.mp3"])


# --------------------------------------------------------------------------
# Integração com o ffmpeg de verdade
# --------------------------------------------------------------------------

HAS_FFMPEG = bool(ss.which_trusted("ffmpeg") and ss.which_trusted("ffprobe"))
REQUIRE_FFMPEG = os.environ.get("SPLITSONG_REQUIRE_FFMPEG") == "1"


@unittest.skipUnless(HAS_FFMPEG or REQUIRE_FFMPEG, "ffmpeg/ffprobe não encontrados no PATH")
class FfmpegIntegrationTest(CliTest):
    def setUp(self):
        super().setUp()
        self.assertTrue(HAS_FFMPEG, "ffmpeg/ffprobe não encontrados no PATH")
        # 3 "músicas" de 10 s (tom de 440 Hz) separadas por 2 s de silêncio
        self.album = self.input / "album.mp3"
        subprocess.run(
            [ss.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "aevalsrc='if(lt(mod(t,12),10),0.5*sin(2*PI*440*t),0)':s=44100:d=34",
             "-c:a", "libmp3lame", "-b:a", "64k", str(self.album)],
            check=True)

    def test_duration(self):
        self.assertAlmostEqual(ss.get_duration(self.album), 34, delta=0.2)

    def test_detect_silences(self):
        silences = ss.detect_silences(self.album, -35, 0.5)
        self.assertEqual(len(silences), 2)
        for (start, end, _), expected in zip(silences, (10, 22)):
            self.assertAlmostEqual(start, expected, delta=0.3)
            self.assertAlmostEqual(end, expected + 2, delta=0.3)

    def test_detect_and_split(self):
        code, out = self.run_main("--detect", "--split", "--min-song", "5")
        self.assertIsNone(code, out)
        tracks = ss.read_log(self.temp / "album")[1]
        self.assertEqual(len(tracks), 3)
        files = sorted((self.output / "album").glob("*.mp3"))
        self.assertEqual([p.name for p in files],
                         ["01 - Faixa 01.mp3", "02 - Faixa 02.mp3", "03 - Faixa 03.mp3"])
        # cortes no meio dos silêncios (11 s e 23 s): 0-11, 11-23, 23-34
        for path, expected in zip(files, (11, 12, 11)):
            self.assertAlmostEqual(ss.get_duration(path), expected, delta=0.5)

    def test_detect_and_split_file_outside_input(self):
        # python split_songs.py --base-dir bin bin/album.mp3 --detect --split
        outside = self.tmp / "album.mp3"
        self.album.replace(outside)
        code, out = self.run_main(str(outside), "--detect", "--split", "--min-song", "5")
        self.assertIsNone(code, out)
        self.assertNotIn("não encontrado", out)
        self.assertEqual(len(list((self.temp / "album").glob("*.mp3"))), 3)
        self.assertEqual(len(list((self.output / "album").glob("*.mp3"))), 3)

    def test_manual(self):
        (self.input / "album.txt").write_text("0;00;11\n0;00;23\n", encoding="utf-8")
        code, out = self.run_main("--manual")
        self.assertIsNone(code, out)
        files = sorted((self.output / "album").glob("*.mp3"))
        self.assertEqual(len(files), 3)
        self.assertAlmostEqual(ss.get_duration(files[0]), 11, delta=0.5)

    def test_extract_sample(self):
        sample = ss.extract_sample(self.album, 1, 3)
        self.assertGreater(len(sample), 1000)


if __name__ == "__main__":
    unittest.main()
