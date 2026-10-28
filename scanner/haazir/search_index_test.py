"""Tests of the search index's words, terms and encodings (haazir.search_index).

    python -m haazir.search_index_test                       # run the tests
    python -m haazir.search_index_test --vectors <file.json> # also write the cases the website's reader must agree on
"""

import json
import sys
import unittest

from . import search_index as si


class Words(unittest.TestCase):
    def test_english(self):
        self.assertEqual(si.words("The State of U.P. v. Ram Kumar & Ors."), ["state", "ram", "kumar"])
        self.assertEqual(si.words("Insurers' claims"), ["insurer", "claim"])
        self.assertEqual(si.words("Business Classes"), ["business", "classe"])  # 'ss' kept; the same rule both sides

    def test_accents_and_marks(self):
        self.assertEqual(si.words("Café Résumé"), ["cafe", "resume"])

    def test_hindi(self):
        self.assertEqual(si.words("दहेज हत्या का मामला"), ["दहेज", "हत्या", "मामला"])
        self.assertEqual(si.words("राम बनाम उत्तर प्रदेश राज्य"), ["राम", "उत्तर", "प्रदेश", "राज्य"])
        self.assertEqual(si.words("धारा ४९८ए।"), ["धारा", "498ए"])  # danda splits, Devanagari digits read as 0-9
        self.assertEqual(si.parse_query("धारा ४९८")["terms"], ["sec:498"])

    def test_other_scripts(self):
        nfkd = lambda xs: [__import__("unicodedata").normalize("NFKD", x) for x in xs]  # noqa: E731
        self.assertEqual(si.words("தமிழ்நாடு"), nfkd(["தமிழ்நாடு"]))  # Tamil vowel signs stay inside the word
        self.assertEqual(si.words("ಕರ್ನಾಟಕ ಹೈಕೋರ್ಟ್"), nfkd(["ಕರ್ನಾಟಕ", "ಹೈಕೋರ್ಟ್"]))

    def test_zero_width(self):
        self.assertEqual(si.words("क्‍ष"), si.words("क्ष"))


class Sections(unittest.TestCase):
    def q(self, s):
        return si.parse_query(s)["terms"]

    def test_us(self):
        self.assertEqual(self.q("u/s 438"), ["sec:438"])
        self.assertEqual(self.q("U/S. 438 Cr.P.C."), ["sec:438", "act:crpc"])
        self.assertEqual(self.q("anticipatory bail 438"), ["anticipatory", "bail", "438"])

    def test_section_of_act(self):
        self.assertEqual(self.q("section 138 of NI Act"), ["sec:138", "act:ni"])
        self.assertEqual(self.q("Section 138 of the N.I. Act"), ["sec:138", "act:ni"])
        self.assertEqual(self.q("section 138 negotiable instruments"), ["sec:138", "act:ni"])

    def test_lists_and_letters(self):
        self.assertEqual(self.q("sections 302/34 IPC"), ["sec:302", "sec:34", "act:ipc"])
        self.assertEqual(self.q("u/s 498-A"), ["sec:498", "sec:498a"])
        self.assertEqual(self.q("u/s 302 r/w 34"), ["sec:302", "sec:34"])
        self.assertEqual(self.q("धारा 498"), ["sec:498"])

    def test_text_side(self):
        t = si.text_terms("Heard on the application under Section 438 Cr.P.C. for anticipatory bail")
        for want in ("sec:438", "act:crpc", "anticipatory", "bail", "438", "section"):
            self.assertIn(want, t)

    def test_district_acts(self):
        self.assertEqual(si.parse_acts("Under Act(s) Under Section(s) INDIAN PENAL CODE 452,323,504 DOWRY PROHIBITION ACT 3 and 4"),
                         [("indian penal code", ["452", "323", "504"]), ("dowry prohibition act", ["3", "4"])])
        self.assertEqual(si.parse_acts("CODE OF CIVIL PROCEDURE order 41,rule 1"), [("code of civil procedure", [])])
        self.assertEqual(si.act_short("NEGOTIABLE INSTRUMENTS ACT"), "ni")
        self.assertEqual(si.act_short("Andhra Pradesh Gaming Act, 1974"), "gaming")
        terms, _, _ = si.doc_district({"cnr": "TSMN000000742018", "case_type": "C.C", "number": 12, "reg_year": 2018,
                                       "acts": "Under Act(s) Under Section(s) NEGOTIABLE INSTRUMENTS ACT 138"}, 2018)
        for want in ("act:ni", "sec:138", "n:12/2018", "t:cc", "negotiable"):
            self.assertIn(want, terms)


class CaseNumbers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(si.parse_case("CRL.A./1628/2023"), ("crla", 1628, 2023))
        self.assertEqual(si.parse_case("BAIL/38919/2025"), ("bail", 38919, 2025))
        self.assertEqual(si.parse_case("WRIT PETITION (CIVIL) No. 906/2016"), ("writpetitioncivil", 906, 2016))
        self.assertEqual(si.parse_case("Crl.A. No. 1628 of 2023"), ("crla", 1628, 2023))
        self.assertEqual(si.parse_case("112 of 2012"), (None, 112, 2012))
        self.assertIsNone(si.parse_case("anticipatory bail 438"))

    def test_query(self):
        p = si.parse_query("CRL.A./1628/2023")
        self.assertEqual(p["keys"], ["crla/1628/2023"])
        self.assertEqual(p["terms"], ["n:1628/2023", "crl"])
        self.assertEqual(si.parse_query("Crl.A. 1628/2023")["keys"], ["crla/1628/2023"])
        self.assertEqual(si.parse_query("bail 1628/2023")["terms"], ["n:1628/2023", "bail"])

    def test_cnr(self):
        p = si.parse_query("UPHC015892432025")
        self.assertEqual(p["cnrs"], ["UPHC015892432025"])
        self.assertEqual(p["terms"], [])
        self.assertEqual(si.cnr_key("uphc-0158-9243-2025"), "UPHC015892432025")
        self.assertIsNone(si.cnr_key("UPHC01589243"))

    def test_hc_doc(self):
        terms, keys, rec = si.doc_hc({"court": "9_13", "court_name": "Allahabad High Court", "case_no": "BAIL/38919/2025",
                                      "petitioner": "DEEPAK", "respondent": "STATE OF U.P.", "cnr": "UPHC015892432025",
                                      "snippet": "CRIMINAL MISC. BAIL APPLICATION No. - 38919 of 2025"}, 2026)
        self.assertEqual(set(keys), {"bail/38919/2025", "UPHC015892432025"})
        for want in ("c:9_13", "s:uttarpradesh", "t:bail", "n:38919/2025", "deepak", "state", "allahabad"):
            self.assertIn(want, terms)
        self.assertEqual(rec["l"], "hc")


class Encoding(unittest.TestCase):
    def test_varint_roundtrip(self):
        ids = [3, 4, 200, 70000, 2 ** 31]
        data, skips = si.encode_postings(ids)
        self.assertEqual(si.decode(data), ids)
        self.assertEqual(skips, [(3, 0)])

    def test_blocks(self):
        ids = list(range(5, 5 + 3 * si.BLOCK * 7, 7))
        for big in (False, True):
            data, skips = si.encode_postings(ids if not big else __import__("numpy").array(ids))
            self.assertEqual(len(skips), 3 if not big else len(range(0, len(ids), si.BLOCK)))
            got = []
            for i, (f, o) in enumerate(skips):
                end = skips[i + 1][1] if i + 1 < len(skips) else len(data)
                blk = si.decode(data[o:end])
                self.assertEqual(blk[0], f)
                got += blk
            self.assertEqual(got, ids)

    def test_numpy_small_path_agree(self):
        import numpy as np
        ids = sorted(set(np.random.default_rng(1).integers(0, 10 ** 8, 5000).tolist()))
        a, sa = si.encode_postings(ids[:2000])
        b, sb = si.encode_postings(np.array(ids[:2000]))
        self.assertEqual(si.decode(a), si.decode(b))
        c, _ = si.encode_postings(np.array(ids))
        self.assertEqual(len(c) > 0, True)

    def test_dict_line(self):
        line, data = si.dict_line("bail", [1, 5, 9], 0)
        self.assertEqual(line, "bail\t3\t=1,5,9")
        self.assertEqual(data, b"")
        ids = list(range(0, 20 * si.BLOCK, 2))
        line, data = si.dict_line("state", ids, 100)
        x = line.split("\t")
        self.assertEqual(x[:3], ["state", str(len(ids)), "100"])
        self.assertEqual(int(x[4]), ids[-1])

    def test_buckets_stable(self):
        self.assertEqual(si.crc("दहेज"), __import__("zlib").crc32("दहेज".encode()))


VECTOR_INPUTS = [
    "anticipatory bail 438", "section 138 negotiable instruments", "ICICI Lombard", "motor accident compensation",
    "u/s 438", "U/S. 438 Cr.P.C.", "section 138 of NI Act", "Section 138 of the N.I. Act", "sections 302/34 IPC",
    "u/s 498-A", "u/s 302 r/w 34", "धारा 498", "दहेज", "दहेज हत्या का मामला", "CRL.A./1628/2023", "Crl.A. No. 1628 of 2023",
    "BAIL/38919/2025", "bail 1628/2023", "UPHC015892432025", "The State of U.P. v. Ram Kumar & Ors.", "Café Résumé",
    "Insurers' claims", "தமிழ்நாடு", "WRIT PETITION (CIVIL) No. 906/2016", "M/s. Shriram City Union Finance Ltd",
    "quashing of FIR 482", "Death Case( M A C T )/1/2026", "dowry death 304B", "u/s 3(1)(x) SC/ST Act",
]
TEXT_INPUTS = [
    "Heard on the application under Section 438 Cr.P.C. for anticipatory bail",
    "JUDICATURE AT ALLAHABAD CRIMINAL MISC. BAIL APPLICATION No. - 38919 of 2025 HON'BLE JITENDRA KUMAR SINHA, J.",
    "Demonetisation � Interpretation of sub-section (2) of s.26 of the Reserve Bank of India Act, 1934",
    "दहेज प्रतिषेध अधिनियम की धारा 3/4 एवं भा.दं.सं. की धारा 498-ए",
    "complaint u/s 138 of the Negotiable Instruments Act, 1881 r/w Section 142",
]


def vectors() -> dict:
    return {"query": {q: si.parse_query(q) for q in VECTOR_INPUTS},
            "words": {q: si.words(q) for q in VECTOR_INPUTS + TEXT_INPUTS},
            "text": {t: sorted(set(si.text_terms(t))) for t in TEXT_INPUTS + VECTOR_INPUTS},
            "crc": {t: si.crc(t) for t in ["bail", "दहेज", "sec:438", "n:1628/2023", "UPHC015892432025"]},
            "cid": {s: si.cid(s) for s in ["9_13", "Kamrup Metro", "Uttar Pradesh", "CAT", "gst-aar"]}}


if __name__ == "__main__":
    if "--vectors" in sys.argv:
        path = sys.argv[sys.argv.index("--vectors") + 1]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(vectors(), fh, ensure_ascii=False, indent=1)
        sys.argv = [sys.argv[0]]
    unittest.main(verbosity=1)
