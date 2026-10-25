"""Checks for the judgment library's reader (haazir.library): acts and sections read from real headnote styles.
Each case says what must be found and what must never be.   python -m haazir.library_check"""

import sys

from .library import tag

CASES = [
    ("Motor Vehicles Act, 1988: Section 166, 168 - Compensation - Multiplier.", ["MV Act"], ["MV Act s. 166", "MV Act s. 168"], []),
    ("Whether the High Court erred in quashing the FIR under Section 13(1)(b) read with Section 13(2) of the Prevention of Corruption Act, 1988",
     ["Prevention of Corruption Act"], ["Prevention of Corruption Act s. 13(1)(b)", "Prevention of Corruption Act s. 13(2)"], []),
    ("Hindu Succession Act, 1956: Section 6 - coparcenary", ["Hindu Succession Act"], ["Hindu Succession Act s. 6"], ["Succession Act"]),
    ("Constitution of India, 1950: Article 22(5) - Preventive Detention", ["Constitution"], ["Constitution Art. 22(5)"], ["Constitution Art. 1950"]),
    ("violates Article 14 and 21 of the Constitution", ["Constitution"], ["Constitution Art. 14", "Constitution Art. 21"], []),
    ("Code of Civil Procedure, 1908 - Order 1 Rule 10 - impleadment", ["CPC"], ["CPC O. 1"], []),
    ("Employees' State Insurance Act, 1948: s. 2(9) - employee", ["ESI Act"], ["ESI Act s. 2(9)"], ["Insurance Act"]),
    ("Consumer Protection Act, 2019 - Section 69 - limitation", ["Consumer Protection Act 2019"], ["Consumer Protection Act 2019 s. 69"], []),
    ("constituted a Constitution Bench to hear the reference", [], [], ["Constitution"]),
    ("cheque dishonoured - Negotiable Instruments Act, 1881 - Section 138", ["NI Act"], ["NI Act s. 138"], []),
    ("Ss. 437 and 439 Cr.P.C. - bail", ["CrPC"], ["CrPC s. 437", "CrPC s. 439"], []),
    ("sections 81 to 86 of the Motor Vehicles Act, 1988", ["MV Act"], [], ["MV Act s. 81to86", "MV Act s. 81"]),
    ("the Insurance Act, 1938 and the Marine Insurance Act, 1963", ["Insurance Act", "Marine Insurance Act"], [], []),
    ("Code of Criminal Procedure, 1973-Section 197-Sanction for prosecution", ["CrPC"], ["CrPC s. 197"], ["CrPC s. 197-S"]),
    ("Penal Code, 1860 – ss.148, 341, 504, 326 r/w.149 – Prosecution case", ["IPC"], ["IPC s. 148", "IPC s. 341"], []),
    ("Penal Code, I 860-Section 302 rlw Section 34-Death caused", ["IPC"], ["IPC s. 302"], []),
    ("amendment to Section 48(1) of the Maharashtra Stamp Act, 1958 governs", [], [], ["Stamp Act"]),
    ("Indian Income Tax Act, 1922-Sub-s. SA of s. 10", [], [], ["Income-tax Act"]),
    ("Income-tax Act, 1961: Section 147 - reassessment", ["Income-tax Act"], ["Income-tax Act s. 147"], []),
    ("offences under Ranbir Penal Code, Samvat 1989", [], [], ["IPC"]),
    # Old scans: full stops for commas, "163-A" for 163A (Pranay Sethi's headnote).
    ("Motor Vehicles Act. 1988 ss. 163-A. 166 and 168 - Computation of compensation", ["MV Act"], ["MV Act s. 163A", "MV Act s. 166", "MV Act s. 168"], []),
    ("Section 5a of the Limitation Act, 1963", ["Limitation Act"], ["Limitation Act s. 5A"], []),
]


def main() -> int:
    failed = 0
    for text, acts, must, never in CASES:
        a, s = tag(text)
        ok = a == acts and all(x in s for x in must) and not any(x in s or x in a for x in never)
        failed += not ok
        if not ok:
            print(f"FAIL {text[:70]}\n     got acts {a}, sections {s}")
    print(f"{len(CASES) - failed} of {len(CASES)} library checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
