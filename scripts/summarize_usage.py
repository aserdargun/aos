"""Render a sanitized model/cost README block from one collector snapshot; never modify files."""

import argparse
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from scripts.record_usage import EFFORTS, FIELDS, MODELS, REPO_ROOT, iso, timestamp, vector


def counterfactual_cost(counts, rates):
    counts = vector(counts)
    if rates is None or counts['cache_write_input_tokens']:
        return None
    amounts = []
    for field in ('input', 'cached_input', 'output'):
        value = rates[field]
        if not isinstance(value, str):
            raise ValueError('Reviewed decimal price strings required')
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 100000:
            raise ValueError('Price is outside reviewed bounds')
        amounts.append(amount)
    return ((counts['input_tokens'] - counts['cached_input_tokens']) * amounts[0]
            + counts['cached_input_tokens'] * amounts[1]
            + counts['output_tokens'] * amounts[2]) / Decimal(1000000)


def render(snapshot, prices):
    if snapshot['schema_version'] != '1' or prices['schema'] != 'aos.api-price-reference.v1':
        raise ValueError('Unsupported accounting schema')
    if snapshot['status'] not in {'observed', 'partial'}:
        raise ValueError('Unsupported observation status')
    totals = vector(snapshot['counts'])
    sessions = snapshot['coverage']['sessions']
    if type(sessions) is not int or sessions < 0:
        raise ValueError('Session count must be observed')
    cutoff = iso(timestamp(snapshot['cutoff']))
    first = iso(timestamp(snapshot['coverage']['first_usage_event']))
    last = iso(timestamp(snapshot['coverage']['last_usage_event']))
    lines = [f'Kayıt kesimi: **{cutoff}**; **{sessions} AOS oturumu**; durum: `{snapshot["status"]}`.',
        f'Token olayı kapsamı: `{first}` → `{last}`. Tüm proje/fatura kapsamı değildir.', '',
        '| Sağlayıcı | Model | Effort | Input | Cached input¹ | Output | Toplam token | API karşılığı² (USD) |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    summed = {field: 0 for field in FIELDS}
    seen, costs, unpriced = set(), [], 0
    for row in snapshot['models']:
        labels = row['provider'], row['model'], row['effort']
        if (labels in seen or labels[0] not in {'openai', 'unknown'}
                or labels[1] not in MODELS | {'unknown'} or labels[2] not in EFFORTS | {'unknown'}):
            raise ValueError('Unknown or duplicate metadata label')
        seen.add(labels)
        counts = vector(row['counts'])
        for field in FIELDS:
            summed[field] += counts[field]
        rates = prices['usd_per_million_tokens'].get(row['model']) if row['provider'] == 'openai' else None
        cost = counterfactual_cost(counts, rates)
        if cost is None:
            unpriced += counts['total_tokens']
        else:
            costs.append(cost)
        lines.append('| ' + ' | '.join([*labels,
            *[f'{counts[field]:,}' for field in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens')],
            'Hesaplanamadı' if cost is None else f'{cost:,.2f}']) + ' |')
    if summed != totals:
        raise ValueError('Model totals differ from snapshot')
    if snapshot['status'] == 'partial':
        lines += ['', '**Kısmi kayıt:** eksik veya belirsiz aralıklar sıfır tüketim değildir.']
    amount = sum(costs, Decimal(0))
    lines += ['', f'**Kaydedilmiş token: {totals["total_tokens"]:,}.** Fiyatlanabilen alt kümenin '
        f'varsayımsal API karşılığı: **{amount:,.2f} USD**; fiyatlanamayan token: **{unpriced:,}**.', '',
        '¹ Cached input, input toplamının alt kümesidir. Reasoning output da output içine dahildir; tekrar toplanmaz.',
        '² Standard / kısa bağlam tarifesiyle karşılaştırma senaryosu; gerçek ücret, abonelik bedeli veya tarihsel fatura değildir.',
        'Gerçek ücret ve abonelik payı **bilinmiyor**. Servis/bağlam sınıfı, geçmiş tarife, vergi, araç, indirim ve donanım giderleri doğrulanmadı.',
        'Goal token/süre sayacı ve yerel runtime tüketimi bu toplama eklenmez.', '',
        '| UTC gün | Kaydedilmiş token |', '|---|---:|']
    daily_total = 0
    seen_days = set()
    for row in snapshot['days']:
        day = date.fromisoformat(row['day']).isoformat()
        if day in seen_days:
            raise ValueError('Duplicate day')
        seen_days.add(day)
        count = vector(row['counts'])['total_tokens']
        daily_total += count
        lines.append(f'| {day} | {count:,} |')
    if daily_total != totals['total_tokens']:
        raise ValueError('Day totals differ from snapshot')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--prices', type=Path, default=REPO_ROOT / 'docs/usage_prices_20261003.json')
    arguments = parser.parse_args()
    try:
        values = []
        for path in (arguments.snapshot, arguments.prices):
            with path.open('rb') as stream:
                content = stream.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ValueError('Accounting input exceeds bound')
            values.append(json.loads(content))
        print(render(*values), end='')
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        parser.exit(1, 'Usage summary unavailable; no private input content printed.\n')


if __name__ == '__main__':
    main()
