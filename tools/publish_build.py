#!/usr/bin/env python3
"""Publish the beneficiary and trust form rule tables as wide files for stepuplaw.com.

Reads data/beneficiary_rules.csv and data/trust_owner_rules.csv (long format, one row per
institution x account type x field) and writes one row per institution x account group:

  publish/beneficiary_rules.json   publish/beneficiary_rules.csv
  publish/trust_accounts.json      publish/trust_accounts.csv
  publish/schema.json              column definitions

Hard rules applied here (see the stepuplaw brief of 2026-09-25):
  1. fl_law_flag is never read into the output, and any detail text beginning "fl:" is cut.
     Detail clauses that compare the document with a Florida statute (655.82, 732.703,
     709.2202, 711.50 to 711.512, "Fla. Stat.", ERISA preemption notes) are internal review
     notes and are removed too.
  2. data/request_list.csv is never read.
  3. Only the institution's own source_url is published. No saved file path is published.
  4. A yes or no is published only with its verbatim quote. Anything else is "not stated".
  5. A value whose source file is not status "saved" in forms.csv, or is marked irrelevant
     in qa_overrides.csv, is dropped to "not stated".

Where an institution has several account types in one group (Traditional and Roth IRA, or
a 401(k) and a 457 form), each field takes the first stated value in block order, with
inherited-IRA blocks last, and records which account type it came from.

  python3 publish/build.py
"""
import csv
import json
import re
import sys
from collections import OrderedDict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUT = ROOT / 'publish'

FL_MAIN = {
    'bank-southstate-bank', 'bank-everbank', 'bank-raymond-james-bank', 'bank-bankunited',
    'bank-city-national-bank-of-florida', 'bank-seacoast-national-bank', 'cu-suncoast',
    'cu-vystar', 'frs-investment', 'frs-pension',
}

BENE_FIELDS = [
    'trust_primary', 'trust_contingent', 'trust_details_required', 'trust_naming_required',
    'trust_docs_requested', 'see_through_certification', 'subtrust_naming_format',
    'per_stirpes_offered', 'per_stirpes_definition', 'per_capita_offered',
    'ldps_or_descendants_allowed', 'banned_language', 'custom_designation_accepted',
    'custom_review_process', 'minors', 'estate_allowed', 'default_beneficiary',
    'successor_beneficiary', 'lapse_rule', 'spousal_consent', 'spousal_consent_mechanics',
    'divorce_revocation_stated', 'max_beneficiaries', 'online_designation',
    'notarization_or_medallion', 'esign_accepted', 'submission_method',
    'stated_processing_time', 'disclaimer_procedure',
]
TRUST_FIELDS = [
    'trust_account_offered', 'certification_accepted', 'full_instrument_required',
    'pages_required', 'trustee_signatures', 'trustee_id', 'tax_id',
    'successor_trustee_proof', 'grantor_death', 'trustee_change',
]
PER_FIELD = ['value', 'detail', 'quote', 'account_type', 'page', 'source_url',
             'form_number', 'revision_date', 'wayback_capture', 'retrieved']

FL_REF = re.compile(
    r'(Fla\.\s*Stat|\b655\.\d|\b732\.703|\b709\.2202|\b711\.5\d|\bpreempt|\bflagg?ed\b|\bflag\b)',
    re.I)


def clean_detail(s: str) -> str:
    """Strip internal Florida-law review notes from a detail string."""
    s = (s or '').strip()
    s = re.split(r'\s*\|?\s*\bfl:', s, maxsplit=1, flags=re.I)[0].strip()
    # parentheticals that mention a Florida statute or a flag
    s = _drop_flagged_parens(s)
    parts = [p.strip() for p in s.split(';')]
    parts = [p for p in parts if p and not FL_REF.search(p)]
    s = '; '.join(parts)
    # no colons in published prose: "at designation: title page" -> "at designation, title page"
    s = re.sub(r'(?<!http)(?<!https):\s+', ', ', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' ;,')
    return s


def _drop_flagged_parens(s: str) -> str:
    """Remove top-level balanced parentheticals that mention a Florida statute or a flag."""
    out, i = [], 0
    while i < len(s):
        if s[i] == '(':
            depth, j = 0, i
            while j < len(s):
                depth += {'(': 1, ')': -1}.get(s[j], 0)
                if depth == 0:
                    break
                j += 1
            group = s[i:j + 1]
            if FL_REF.search(group):
                while out and out[-1] == ' ':
                    out.pop()
            else:
                out.append(group)
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return ''.join(out)


def page_of(locator: str) -> str:
    m = re.search(r'\bp(\d+)\b', locator or '')
    if m:
        return m.group(1)
    return 'web page' if 'web page' in (locator or '') else ''


def wayback_of(r: dict) -> str:
    m = re.search(r'Wayback capture (\d{4}-\d{2}-\d{2})', r.get('locator', ''))
    if m:
        return m.group(1)
    c = (r.get('capture_date') or '').strip()
    if re.fullmatch(r'\d{8}', c):
        return f'{c[:4]}-{c[4:6]}-{c[6:]}'
    return c


# Reviewed 2026-09-25: quotes too weak to publish (label only, or garbled extraction).
WEAK = {('ally', 'per_stirpes_offered'), ('pen-calpers', 'banned_language')}


def load_forms():
    forms = {f['file']: f for f in csv.DictReader(open(DATA / 'forms.csv'))}
    irrelevant = {q['file'] for q in csv.DictReader(open(DATA / 'qa_overrides.csv'))
                  if (q.get('status') or '').strip() == 'irrelevant'}
    return forms, irrelevant


def build(fn: str, fields: list, stats: Counter):
    forms, irrelevant = load_forms()
    rows = list(csv.DictReader(open(DATA / fn)))
    blocks = OrderedDict()  # (inst, group) -> OrderedDict(account_type -> {field: row})
    meta = {}
    for r in rows:
        key = (r['inst_id'], r['account_group'])
        blocks.setdefault(key, OrderedDict()).setdefault(r['account_type'], {})[r['field']] = r
        meta[key] = (r['institution'], r['segment'])
    out = []
    for key, types in blocks.items():
        inst_id, group = key
        order = sorted(types.keys(), key=lambda t: (1 if t.lower().startswith('inherited') or 'inherited ira' in t.lower() else 0))
        rec = OrderedDict()
        rec['inst_id'] = inst_id
        rec['institution'] = meta[key][0]
        rec['segment'] = meta[key][1]
        rec['account_group'] = group
        rec['account_types'] = list(types.keys())
        rec['florida'] = inst_id.startswith(('flbank-', 'flcu-')) or inst_id in FL_MAIN
        rec['fields'] = OrderedDict()
        for f in fields:
            chosen = None
            seen_vals = set()
            for t in order:
                r = types[t].get(f)
                if not r:
                    continue
                v = r['value'].strip()
                if v not in ('yes', 'no'):
                    continue
                if not r['quote'].strip():
                    stats['dropped_no_quote'] += 1
                    continue
                if (inst_id, f) in WEAK:
                    stats['dropped_weak_quote'] += 1
                    continue
                src = r['source_file'].strip()
                st = forms.get(src, {}).get('status', '')
                if st != 'saved' or src in irrelevant:
                    stats['dropped_irrelevant_or_unsaved'] += 1
                    continue
                seen_vals.add(v)
                if chosen is None:
                    chosen = r
            if chosen is None:
                continue
            if len(seen_vals) > 1:
                stats['field_differs_across_account_types'] += 1
            raw_detail = chosen['detail']
            d = clean_detail(raw_detail)
            if d != re.sub(r'(?<!http)(?<!https):\s+', ', ', raw_detail.strip()).strip(' ;,'):
                stats['detail_florida_note_removed'] += 1
            rec['fields'][f] = OrderedDict(
                value=chosen['value'].strip(),
                detail=d,
                quote=chosen['quote'].strip(),
                account_type=chosen['account_type'],
                page=page_of(chosen['locator']),
                source_url=chosen['source_url'].strip(),
                form_number='' if chosen['form_number'].strip() == 'not printed' else chosen['form_number'].strip(),
                revision_date='' if chosen['revision_date'].strip() == 'not printed' else chosen['revision_date'].strip(),
                wayback_capture=wayback_of(chosen),
                retrieved=chosen['retrieved'].strip(),
            )
        out.append(rec)
    out.sort(key=lambda r: (r['account_group'], r['institution'].lower()))
    return out


def write_csv(path: Path, rows: list, fields: list):
    head = ['inst_id', 'institution', 'segment', 'account_group', 'account_types', 'florida']
    for f in fields:
        head += [f if k == 'value' else f'{f}_{k}' for k in PER_FIELD]
    with open(path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in rows:
            line = [r['inst_id'], r['institution'], r['segment'], r['account_group'],
                    '; '.join(r['account_types']), 'yes' if r['florida'] else 'no']
            for f in fields:
                x = r['fields'].get(f)
                for k in PER_FIELD:
                    if x is None:
                        line.append('not stated' if k == 'value' else '')
                    else:
                        line.append(x[k])
            w.writerow(line)


def questions(fn: str):
    q = OrderedDict()
    for r in csv.DictReader(open(DATA / fn)):
        q.setdefault(r['field'], r['question'])
    return q


def main():
    stats_b, stats_t = Counter(), Counter()
    bene = build('beneficiary_rules.csv', BENE_FIELDS, stats_b)
    trust = build('trust_owner_rules.csv', TRUST_FIELDS, stats_t)

    insts = {i['inst_id']: i for i in csv.DictReader(open(DATA / 'institutions.csv'))}
    with_rules = {r['inst_id'] for r in bene} | {r['inst_id'] for r in trust}
    no_form = sorted(
        ({'inst_id': k, 'institution': v['institution'], 'segment': v['segment'],
          'florida': k.startswith(('flbank-', 'flcu-')) or k in FL_MAIN}
         for k, v in insts.items() if k not in with_rules),
        key=lambda x: x['institution'].lower())

    # Safety net: nothing Florida-flag-like may leave this script.
    blob = json.dumps(bene) + json.dumps(trust)
    for bad in ('fl_law_flag', '"fl:', ' fl:', '| fl:'):
        if bad in blob:
            sys.exit(f'build.py: forbidden text {bad!r} in output')
    for rows in (bene, trust):
        for r in rows:
            for f, x in r['fields'].items():
                if FL_REF.search(x['detail']):
                    sys.exit(f'build.py: Florida review note left in {r["inst_id"]} {f}: {x["detail"]}')

    retrieved = sorted({x['retrieved'] for rows in (bene, trust) for r in rows for x in r['fields'].values() if x['retrieved']})
    common = OrderedDict(
        title='Beneficiary Designation and Trust Account Rules at US Financial Institutions',
        publisher='StepUp Law (Klagge Law, PLLC)',
        license='CC BY 4.0',
        retrieved_from=retrieved[0] if retrieved else '', retrieved_to=retrieved[-1] if retrieved else '',
        institutions_checked=len(insts),
    )
    OUT.mkdir(exist_ok=True)
    json.dump(OrderedDict(common, rows=bene, no_public_form=no_form), open(OUT / 'beneficiary_rules.json', 'w'), indent=1, ensure_ascii=False)
    json.dump(OrderedDict(common, rows=trust), open(OUT / 'trust_accounts.json', 'w'), indent=1, ensure_ascii=False)
    write_csv(OUT / 'beneficiary_rules.csv', bene, BENE_FIELDS)
    write_csv(OUT / 'trust_accounts.csv', trust, TRUST_FIELDS)

    qb, qt = questions('beneficiary_rules.csv'), questions('trust_owner_rules.csv')
    per = OrderedDict(
        value='yes, no or not stated. A yes or no always carries a verbatim quote from the institution\'s own document. "Not stated" means the public documents read are silent. It does not mean the option is refused.',
        detail='A short note on what the document says, written by the compiler.',
        quote='The institution\'s own words, verbatim (whitespace, quotation marks and hyphenation normalized; "..." marks omitted text).',
        account_type='The account type or form the value was read from, where the institution has more than one in the group.',
        page='Page of the document the quote is on, or "web page".',
        source_url='The institution\'s own URL for the document. Where the live URL refused scripted requests, the quote was read from an Internet Archive (Wayback Machine) capture of that URL.',
        form_number='The form number printed on the document, if any.',
        revision_date='The revision or edition date printed on the document, if any.',
        wayback_capture='Date of the Wayback Machine capture the quote was read from, if not read live.',
        retrieved='Date the document was retrieved.',
    )
    schema = OrderedDict(
        common,
        files=OrderedDict([
            ('beneficiary_rules.csv / .json', 'Beneficiary designation rules, one row per institution and account group.'),
            ('trust_accounts.csv / .json', 'Rules for an account owned by or retitled to a trust, one row per institution and account group.'),
        ]),
        account_groups=OrderedDict(
            ira='Traditional, Roth, SEP, SIMPLE and inherited IRAs', employer_plan='401(k), 403(b), 457, 401(a) and the TSP',
            pension='Defined benefit pension systems', brokerage_tod='Brokerage transfer on death (TOD) registration',
            bank_pod='Bank and credit union payable on death (POD) accounts', annuity='Annuity contracts',
            life='Life insurance', hsa='Health savings accounts'),
        row_columns=OrderedDict(
            inst_id='Stable identifier for the institution', institution='Institution name', segment='brokerage, bank, credit_union, recordkeeper, insurer, pension or hsa',
            account_group='See account_groups', account_types='Account types covered (semicolon separated in the CSV)',
            florida='yes if the institution is headquartered in Florida or is a Florida state plan'),
        per_field_columns=per,
        beneficiary_fields=OrderedDict((f, qb.get(f, '')) for f in BENE_FIELDS),
        trust_account_fields=OrderedDict((f, qt.get(f, '')) for f in TRUST_FIELDS),
        csv_naming='In the CSV each field appears as <field> (the value) followed by <field>_detail, <field>_quote, <field>_account_type, <field>_page, <field>_source_url, <field>_form_number, <field>_revision_date, <field>_wayback_capture and <field>_retrieved.',
    )
    json.dump(schema, open(OUT / 'schema.json', 'w'), indent=1, ensure_ascii=False)

    nb = sum(len(r['fields']) for r in bene)
    nt = sum(len(r['fields']) for r in trust)
    print(f'beneficiary: {len(bene)} rows, {len({r["inst_id"] for r in bene})} institutions, {nb} quoted values; {dict(stats_b)}')
    print(f'trust: {len(trust)} rows, {len({r["inst_id"] for r in trust})} institutions, {nt} quoted values; {dict(stats_t)}')
    print(f'no public form: {len(no_form)} of {len(insts)} institutions')


if __name__ == '__main__':
    main()
