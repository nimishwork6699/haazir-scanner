"""Checks of the OCR step's readability rule (haazir.ocr), on real pages from the library's stored text:
garbled pages must be caught, readable ones must be left alone, and rows must be picked (or not) for OCR.

    python -m haazir.ocr_check          # prints each check; exit code 1 if any fails

The samples are pieces of stored text as pdftotext read them (26-28 Oct 2026). When the rule changes, add the page that
showed the problem here first.
"""

import sys

from . import ocr

# ---------------------------------------------------------------- pages that are NOT words (must be OCR'd)
GARBLED = {
    "Krutidev Hindi (RGIPT service rules, rules/central/2017)":
        "ds deZpkfj;ksa ds fy, fuEu çdkj dh Nqêh Lohdk;Z gksxh% da½ lkekU; Nqêh( [k½ fo'ks\"k lkekU; Nqêh( x½ vk/ks osru "
        "okyh Nqêh( ?k½ :ikarfjr vodk'k( M½ vftZr vodk'k( p½ ekr`Ro vodk'k( N½ firR` o vodk'k( t½ vuftZr Nqêh( >½ vlk/kkj.k "
        "Nqêh( ¥½ v/;;u vodk'k( IndiaCode V½ v/;;u vodk'k( B½ cPpk xksn yus s ds fy, vodk'kA lkekU; Nqêh lsok vkSj drZO; "
        "}kjk vftZr ugha dh tkrh gAS bl rjg dh Nqêh ds fy, vof/k ,d dSyMsa j o\"kZ eas vkB fnuksa rd lhfer gS vkSj bUgsa bl "
        "rjg fn;k tkrk gS fd vodk'k lers vuqifLFkfr dk dqy le; ukS fnu ls T;knk ugha gksrk gSA lkekU; Nqêh dks fdlh vU; "
        "çdkj ds vodk'k ds lkFk ugha tksM+k tk ldrk gSA",
    "Krutidev Hindi (Indian Nursing Council recruitment rules)":
        "ij fu;fq Dr ds ckn 5 o\"kZ dh fu;fer los k( vkSj [k½ dkye 7 eas lh/kh HkrhZ okyksa ds fy, fu/kkfZ jr vfuok;Z vgZrk,¡ "
        "vkSj vuHq ko j[kus okys vf/kdkjhA fVIi.kh 1% çfrfu;qfDr@lafonk dh vof/k lkekU;r% rhu o\"kZ ls vf/kd ugha gkxs h] "
        "ftlesa mlh vFkok fdlh vU; lxa Bu@foHkkx eas vFkok dUs æ@jkT; ljdkj ds v/khu fdlh Hkh lLa Fkku esa bl fu;fq Dr ls "
        "rRdky iwoZ /kkfjr laox&Z cká in ij çfrfu;fq Dr dh vof/k 'kkfey gkxs hA fVIi.kh 2% çfrfu;fq Dr ij fu;Dq r fd, tkus "
        "okys çkFkhZ dh vf/kdre vk;&q lhek vkons u&i= dh çkfIr dh vfUre rkjh[k dks 56 o\"kZ ls vf/kd ugha gksuh pkfg,A",
    "Hindi in a font mapped to the wrong code points (notification 2016-04)":
        "᭭वामी के कारण है कᳱ ᮧ᭭तुित अविध के आबटं न के ᱨप मᱶ लाभ हािन िववरण मᱶ होगी। पवू ᭅ उि᭨लिखत खुलासे कु ल ᳞ापाक आय हते ु "
        "सा᭥या मᱶ बदलाव के िववरण मᱶ भी ᳰकए जाएगं ।े भारतीय लखे ा मानकᲂ कᳱ आव᭫यकता हते ु खलु ासे के अितᳯरᲦ पवू ᭅ उि᭨लिखत "
        "खुलासे अᮄय ᳞ापक आय के संबंध मᱶ भी ᳰकए जाएंग।े )(ii तुलनपᮢ एवं सा᭥या मᱶ बदलाव, सा᭥या के भीतर का िववरण भी मूल के "
        "᭭वामी कᳱ सा᭥या से पथृ क ᱨप से ᮧ᭭ततु ᳰकया जाएगा। (iii) सा᭥या तरीके के ᮧयोग हते ु िविनधानᲂ का कारण बताना",
    "Tamil in a legacy font (Constitution, Tamil edition)":
        "ÏªÂa muR r£l« k‰W« ÚÂ mik¢rf« IndiaCode ÏªÂa murik¥ò (2025, m¡nlhg® 31 tiu ÂU¤j« brŒa¥g£lJ) THE CONSTITUTION OF "
        "INDIA (As amended upto 31st October, 2025) jäHh¡f«: khãy¢ r£l M£Ábkhê Miza« lh¡l®. m«ng¤f® r£l¥gšfiy¡fHf tshf«, "
        "“ó«bghêš” gRiktê¢ rhiy, br‹id – 600 028. 2025 khãy¢ r£l M£Ábkhê Miza« jäœehL muR r£l¤Jiw¢ brayhs® : ÂU. Á. "
        "#h®{ mby¡rh©l® Ind ika muR ia r£l Mnyhrf® : CodeÂU. r. knAZ ghò jiyt® ÚÂaur® ÂUkÂ Ïuh. jhuâ, kh©òäF nkdhŸ ÚÂgÂ",
    "Kannada in the Nudi legacy font (Karnataka act, library/state/karnataka)":
        "P£À ÁðlPÀ PÀø¶ ¸AÀ §A¢ü QÃlUÀ¼ÄÀ ªÄÀ vÀÄÛ gÉÆÃUÀU¼À À C¢ü¤AiÀÄªÀÄ, 1968 (PÀ£ÁðlPÀ gÁdå¥Àv,æÀ UÀÄgÄÀ ªÁg,À ªÄÉ Ã 11, "
        "2006 gAÀ zÄÀ 713 jAzÀ 725gÀ ªgÀ VÉ £À ¥ÄÀ lUÀ¼À°è ¥æÀPlÀ ªÁVz.É ) ¥ÀæPgÀ tÀ U¼À À C£ÀÄPÀæªÄÀ tPÂ É ¥PæÀ ÀgÀtUÀ¼ÀÄ: "
        "1. aPÀÌ ºÉ¸ÀgÄÀ , ªÁå¦Û ªÀÄvÄÀ Û ¥ÁægÀA¨sÀ 2. ¥Àj¨Ás µÉU¼À ÀÄ 3. QÃlU",
    "an old, bad OCR layer (Gauhati High Court criminal rules)":
        "Akotr d I (d o og q oE od E IF$t IFi= H o! 6 (E , .oq oo a a q) o C) .Er doao o li d O. F 0) C) lr (g C) oX oo -i (d "
        "(,) od o Io oX iFl o O g c.) o I h0 o,+J d +) Pd o ,o Li bo F. oot< E oH o -ti E oo o --. O a F & p. s-$s ilsifu "
        "$E..iHn=E oCk) c) bo 0) ! C) dN oBo o ok o Jr C) oo o q) o zH + o lr o o H (U +J F o i c0 iFl o. a a& x! *3E P q; "
        "Ei[ EvHo:3i6Br ,E. $E ;E H3 Eg 5t HH .E& *3 gEE Tt I EslEiigii$;i gs ,3iu [i EI i, z! ra.l & .l z & U",
    "a Hindi table read through a broken font (Bihar minimum wages)":
        "TT H1IFf fTIT 1NtzI.iI I ii ii. fr— 'lA-I' 3 L0 3t0— - 1c1 1- 1948 (1948 CM ii) TT-5 1TT c1Ic1 3M TPT-3 1T ccc1 "
        "Ih11') CM HI Cf R1k ixii t)-1)c N-2 ct4iR.ui z fr T/ff 3Tf 1ci ill P1ici ft1I 1vui lc11 4-NIc) cl c tR R c1'1u1c "
        "-iii c'1H[ C cj) cl1 I 3itiI —i() N —02 -ii -J' cb44JR) R-i'1- 1 -)—i(3T) —O cici IT- ZJ{ IIc1 '1 3F1 -1 L) -II "
        "rT/ ri *ii fflI 3TJ J, fitii— 2- 0 *— fTf— 1TT5 1T-T ifI 3ET &f1fT IsZJYIc'j 3TT &—i()* PT-2 9cFI E Tf3 34f5f "
        "3Tftr 3TfiZTTT, 1948 (1948 5T ii) i1i1ui 5T 111' I i1ici ffiThr fjzjf",
}

# ---------------------------------------------------------------- pages that read well (must NOT be OCR'd)
READABLE = {
    "English act text (Minimum Wages Act, Delhi)":
        "riate Government” means,— (i) in relation to any scheduled employment carried on by or under the authority of the "
        "4[Central Government or a railway administration], or in relation to a mine, oilfield or major port, or any "
        "corporation established by 5[a Central Act], the Central Government, and (ii) in relation to any other scheduled "
        "employment, the State Government; 6[(bb) “child” means a person who has not completed his fourteenth year of age;] "
        "(c) “competent authority” means the authority appointed by the appropriate Government by notification in its "
        "Official Gazette to ascertain from time to time the cost of living index number applicable to the employees",
    "English tribunal decision (TDSAT 2012)":
        "Mr.Singh would contend that the Petitioner was not aware of the remedies available to it. It is difficult to accept "
        "the said contention as even in its letter dated 29.3.2011, the Petitioner alleged violation of the provisions of "
        "the Competition Act and threatened the Respondent no.1 that it would approach the concerned authority of the Central "
        "Government for cancellation of the permission granted in its favour. In any event the Petitioner cannot be "
        "permitted to raise a contention before this Tribunal that despite the fact that the Regulator in 2009 specifically "
        "provided that all agreements shall be in writing; on what premise an oral agreement between the parties was entered",
    "Unicode Hindi (Law Commission report)":
        "ेश्यों की सेवा के लिए है उनका उल्लंघन या अतिक्रमण करने के लिए नहीं। किसी पीड़ित पक्ष को प्रस्तावित. अधिकरण के "
        "निर्णय के विरुद्ध अनुच्छेद 226 के अधीन रिंट याचिका दायर करने का अधिकार नहीं ' होगा क्योंकि यह निर्धारित विधि है-कि "
        "जहाँ अपील करने का पर्याप्त उपचार उपलब्ध है वहाँ भारत के संविधान के अनुच्छेद 226 के अधीन उच्च न्यायालय मैं रिंट "
        "याचिका दायर करने का अधिकार नहीं होगा। इस प्रकार जब किसी. पीड़ित व्यक्ति को प्रस्तात्ित अधिकरण के अन्तिम निर्णय",
    "Gazette masthead in Krutidev, the rest English and Hindi (G.S.R. 808 of 2016)":
        "jftLVªh löa Mhö ,yö&33004@99 REGD. NO. D. L.-33004/99 la- 577] No. 577] vlk/kj.k EXTRAORDINARY Hkkx II—[k.M "
        "3—mi&[k.M (i) PART II—Section 3—Sub-section (i) izkf/dkj ls izdkf'kr PUBLISHED BY AUTHORITY de ubZ fnYyh] "
        "'kfuokj] vxLr 20] 2016@Jko.k 29] 1938 o NEW DELHI, SATURDAY, AUGUST 20, 2016/SRAVANA 29, 1938 iaC कृ िष और "
        "कसान क याण मं ालय d (कृ िष, सहका रता और कसान क याण िवभाग) In अिधसचू ना नई द ली, 17 अग त, 2016 सा.का.िन. "
        "808(अ).—क टनाशी िनयम, 1971 का और संशोधन करने के िलए िन िलिखत ा प िनयम, िजसे के ीय सरकार, क टनाशी "
        "अिधिनयम,1968 (1968 का 46) क धारा 36 ारा द शि य का योग करते ए, बनाने का ताव करती ह,ै िजसम उन सभी",
    "English preface after a mixed-up cover line (Constitution, Nepali edition)":
        "IndiaCode THE COभ[NनाSरोभतTमे I्कबTर Uो२I०Tसn२Iंd५वOि-iaसNधCम्Oमाoन]FdIeNDIA [As on November, 2025] 2025 भारत "
        "सरकार विधध अवन न्याय मन्त्रालय विधायी विभाग, राजभाषा पक्ष GOVERNMENT OF INDIA MINISTRY OF LAW AND JUSTICE "
        "LEGISLATIVE DEPARTMENT, OFFICIAL LANGUAGES WING PREFACE It is with great pride that we present the second edition "
        "of the Constitution of India in the Nepali language. This edition has been prepared by the National Translation "
        "Mission (NTM), Central Institute of Indian Languages (CIIL), Mysuru on behalf of the Official Languages Wing",
}

ENGLISH, KRUTI = READABLE["English tribunal decision (TDSAT 2012)"], GARBLED["Krutidev Hindi (RGIPT service rules, rules/central/2017)"]


def rows() -> list[tuple[str, dict, bool]]:
    """(what, row, should it be OCR'd)"""
    return [
        ("(a) website PDF, scan-only", {"title": "Report No. 95", "text": "", "pages": 48, "scan_only": True,
                                         "file": "library/law-commission/10-tenth/report-no-95.pdf", "store": "drive"}, True),
        ("(a) website PDF, good text", {"title": "Synopsis form", "text": ENGLISH, "pages": 3, "scan_only": False,
                                         "file": "formats/hc/patna/form.pdf", "store": "b2"}, False),
        ("(a) website PDF, Krutidev text", {"title": "Circular", "text": KRUTI, "pages": 2, "scan_only": False,
                                            "file": "court-rules/delhi/c.pdf", "store": "b2"}, True),
        ("(a) website page (HTML): nothing to OCR", {"title": "Notice", "text": "", "scan_only": False,
                                                      "file": "court-rules/delhi/notice.html", "store": "b2"}, False),
        ("(a) already OCR'd", {"text": ENGLISH, "scan_only": True, "text_source": "ocr", "file": "x/y.pdf", "store": "b2"}, False),
        ("(a) PDF that could not be read before", {"text": "", "scan_only": True, "ocr_error": "BadPDF: no pages",
                                                    "file": "x/y.pdf", "store": "b2"}, False),
        ("(b) India Code, Krutidev main file", {"title": "Rules, 2017", "text": KRUTI, "text_status": "ok", "main_file": "a.pdf",
                                                "files": [{"name": "a.pdf", "store": "drive", "zip": "library/rules/central/2017.pdf-1.zip",
                                                           "offset": 77, "size": 1000, "pages": 3}]}, True),
        ("(b) India Code, scan only", {"title": "Notification", "text": None, "text_status": "scan_only", "main_file": "n.pdf",
                                       "files": [{"name": "n.pdf", "store": "drive", "zip": "z.zip", "offset": 10, "size": 5}]}, True),
        ("(b) India Code, good English", {"title": "Rules, 2019", "text": ENGLISH, "text_status": "ok", "main_file": "a.pdf",
                                          "files": [{"name": "a.pdf", "store": "drive", "zip": "z.zip", "offset": 1, "size": 9}]}, False),
        ("(b) India Code, no original file", {"title": "Order", "text": None, "text_status": "no_file", "files": []}, False),
        ("(c) tribunal decision, scanned", {"case_number": "Appeal 1 of 2010", "text": "", "scan": True, "pages": 12,
                                            "file": "library/tribunals/aptel/2010/a.pdf", "store": "drive"}, True),
        ("(c) tribunal decision, text", {"case_number": "Appeal 2 of 2010", "text": ENGLISH, "scan": False, "pages": 4,
                                         "file": "library/tribunals/aptel/2010/b.pdf", "store": "drive"}, False),
        ("Constitution in Santhali (Ol Chiki): no OCR model", {"title": "The Constitution of India in Santhali", "text": None,
                                                               "text_status": "scan_only", "main_file": "s.pdf",
                                                               "files": [{"name": "s.pdf", "store": "drive", "zip": "z.zip", "offset": 1, "size": 9}]}, False),
    ]


def main() -> int:
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")

    print("Pages that must be OCR'd:")
    for what, text in GARBLED.items():
        v = ocr.page_verdict(text)
        check(v == "garbled" and not ocr.readable(text), f"{what}: {v} {dict(ocr.page_stats(text))}")
    print("Pages that must be left alone:")
    for what, text in READABLE.items():
        v = ocr.page_verdict(text)
        check(v == "ok" and ocr.readable(text), f"{what}: {v} {dict(ocr.page_stats(text))}")
    print("Whole documents:")
    check(ocr.page_verdict("") == "empty" and not ocr.readable(""), "empty text: a scan")
    check(ocr.readable("\f".join([ENGLISH] * 12 + [KRUTI])), "12 English pages and 1 Krutidev page: readable (a Gazette's Hindi cover)")
    check(not ocr.readable("\f".join([ENGLISH] * 3 + [KRUTI] * 2)), "3 English pages and 2 Krutidev pages: needs OCR")
    check(not ocr.readable("\n".join([ENGLISH, KRUTI, KRUTI])), "no page breaks (India Code text), a third English: needs OCR")
    print("Rows picked for OCR:")
    for what, row, want in rows():
        check(ocr.needs_ocr(row) == want, f"{what}: {'OCR' if want else 'leave'}")
    ref = ocr.pdf_ref(rows()[6][1])
    check(ref == {"store": "drive", "zip": "library/rules/central/2017.pdf-1.zip", "offset": 77, "size": 1000, "name": "a.pdf", "pages": 3},
          "India Code row: its PDF is a ranged read of the zip")
    print("Language of the OCR:")
    for title, lang, want in [("The Constitution of India in Tamil", None, "tam+eng"), ("The Constitution of India in Santhali Devanagari script", None, "hin+eng"),
                              ("The Constitution of India in Santhali", None, None), ("Rules, 2017", None, "hin+eng"),
                              ("Report No. 95", "English", "hin+eng"), ("The Constitution of India in Marathi", None, "mar+hin+eng"),
                              ("Notification in exercise of powers", None, "hin+eng")]:
        got = ocr.ocr_langs({"title": title, "language": lang})
        check(got == want, f"{title!r}: {got}")
    for state, want in [("Karnataka", "kan+eng"), ("Tamil Nadu", "tam+eng"), ("West Bengal", "ben+eng"), ("Uttar Pradesh", "hin+eng"),
                        ("CENTRAL", "hin+eng"), ("Maharashtra", "mar+eng")]:
        got = ocr.ocr_langs({"title": "THE SRINIVAS UNIVERSITY ACT, 2013", "state": state})
        check(got == want, f"a state act of {state}: {got}")
    print("Writing back:")
    row = {"title": "t", "text": "", "scan_only": True, "pages": 2, "file": "a.pdf", "store": "b2", "sha1": "x"}
    new = ocr.apply(row, {"pages": ["first page", "second page"], "ocr_pages": 2, "pages_total": 2, "capped": False, "failed_pages": 0})
    check(new["text"] == "first page\n\f\nsecond page" and new["text_source"] == "ocr" and new["ocr_pages"] == 2
          and all(new[k] == v for k, v in row.items() if k != "text"), "a website row keeps every field, gains text, text_source, ocr_pages")
    ic = {"title": "t", "text": None, "text_status": "scan_only", "files": [{"name": "a.pdf"}]}
    new = ocr.apply(ic, {"pages": ["line one\n\nline  two", "three"], "ocr_pages": 2, "pages_total": 400, "capped": True, "failed_pages": 0})
    check(new["text"] == "line one\nline two\nthree" and new["ocr_capped"] == ocr.MAX_PAGES and new["ocr_pages_total"] == 400
          and new["text_status"] == "scan_only", "an India Code row: lines as India Code keeps them; the cap is recorded")
    print(f"{'All checks pass' if not fails else f'{fails} checks FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
