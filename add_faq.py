#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Add FAQ sections and JSON-LD to 10 HTML files.
"""

import re
import os

BASE = '/Users/shoichionizuka/sellersprite-lp-'

# ─── CSS to inject (before /* QUOTE SECTION */ or before first @media) ────────
FAQ_CSS = '''/* FAQ SECTION */
.faq-section{background:#fff;padding:64px 24px;}
.faq-inner{max-width:760px;margin:0 auto;}
.faq-section h2{font-size:1.6em;font-weight:bold;text-align:center;margin-bottom:32px;color:#1a1a1a;}
.faq-list{display:flex;flex-direction:column;gap:12px;}
.faq-item{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,0.07);overflow:hidden;}
.faq-q{width:100%;background:none;border:none;padding:20px 24px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;text-align:left;font-size:16px;font-weight:700;color:#1a1a1a;line-height:1.5;}
.faq-icon{font-size:22px;font-weight:400;color:#FB8C1E;flex-shrink:0;margin-left:12px;transition:transform 0.2s;}
.faq-a{max-height:0;overflow:hidden;transition:max-height 0.3s ease,padding 0.3s ease;}
.faq-a-inner{padding:0 24px 20px;font-size:15px;line-height:1.8;color:#555;}
.faq-item.open .faq-a{max-height:400px;}
.faq-item.open .faq-icon{transform:rotate(45deg);}
'''

# ─── JS to inject (inside existing <script> block) ───────────────────────────
FAQ_JS = '''function toggleFaq(btn){
  var item=btn.closest('.faq-item');
  var isOpen=item.classList.contains('open');
  item.classList.toggle('open',!isOpen);
  btn.setAttribute('aria-expanded',!isOpen);
}'''

# ─── FAQ data per file ────────────────────────────────────────────────────────
FAQ_DATA = {
    'index.html': [
        ('セラースプライトとは何ですか？',
         'Amazonセラー向けのリサーチツールです。商品リサーチ・キーワード分析・市場調査・ライバル調査など、Amazon物販に必要な分析が一つのツールでできます。'),
        ('初心者でも使えますか？',
         'はい。日本語対応で、直感的に操作できます。無料プランもあるので、まず触って試すのがおすすめです。'),
        ('割引コード「CJ9852」で何が安くなりますか？',
         '全ての有料プランが30%OFFになります。登録時にコードを入力するだけで適用されます。'),
        ('他のツール（Helium10やJungle Scout）との違いは？',
         'セラースプライトは日本語完全対応で、日本のAmazon市場に特化したデータが豊富です。日本で物販をするなら最も使いやすい選択肢です。'),
    ],
    'hyouban.html': [
        ('セラースプライトの評判は実際どうですか？',
         'Amazon物販セラーの間では「リサーチ精度が高い」「日本語で使いやすい」と評価されています。特にキーワード分析の精度に定評があります。'),
        ('悪い評判はありますか？',
         '「機能が多すぎて最初は迷う」という声があります。ただ、よく使う機能は3〜4つに絞られるので、慣れれば問題ありません。'),
        ('無料版と有料版で精度は違いますか？',
         'データの精度自体は同じです。有料版では検索回数の制限がなくなり、より多くの機能が使えるようになります。'),
    ],
    'coupon.html': [
        ('セラースプライトのクーポンコードは本当に使えますか？',
         'はい。「CJ9852」は公式代理店コードで、登録画面で入力すれば30%OFFが即適用されます。'),
        ('クーポンコードに有効期限はありますか？',
         'ありません。公式代理店として常時提供しているコードなので、いつでも使えます。'),
        ('他のクーポンと併用できますか？',
         '基本的に割引コードは1つのみ適用可能です。CJ9852が最大割引率（30%OFF）なので、これ一つで十分です。'),
    ],
    'muryou.html': [
        ('セラースプライトの無料版でできることは？',
         'キーワードリサーチや商品リサーチの基本機能が使えます。1日の検索回数に制限がありますが、ツールの使い勝手を試すには十分です。'),
        ('無料版から有料版への切り替えは簡単ですか？',
         'はい。アカウント設定からプランを選ぶだけです。切り替え時に割引コード「CJ9852」を入力すれば30%OFFになります。'),
        ('クレジットカードなしで無料登録できますか？',
         'はい。無料会員登録にクレジットカードは不要です。メールアドレスだけで始められます。'),
    ],
    'waribiki.html': [
        ('セラースプライトを最も安く始める方法は？',
         '割引コード「CJ9852」を登録時に入力してください。全プラン30%OFFで始められます。'),
        ('30%OFFだと実際いくらになりますか？',
         'スタンダード月払いの場合、¥13,998が約¥9,799/月になります。年間で約¥50,000の節約です。'),
        ('学生割引や法人割引はありますか？',
         '公式の学生・法人割引は現時点ではありません。CJ9852の30%OFFが最大の割引です。'),
    ],
    'kuchikomi.html': [
        ('セラースプライトの口コミは信頼できますか？',
         'このページでは実際のユーザーレビューや物販メディアの評価を集めています。良い点だけでなく気になった点も含めて紹介しています。'),
        ('実際に使って売上は上がりますか？',
         'ツール自体が売上を保証するものではありませんが、データに基づいた仕入れ判断ができるようになるため、的中率は確実に上がります。'),
        ('どんな人に向いていますか？',
         'Amazon物販をしている人、これから始める人の両方に向いています。特にリサーチに時間がかかっている人には効果が大きいです。'),
    ],
    'touroku.html': [
        ('登録に必要なものは？',
         'メールアドレスだけです。無料会員ならクレジットカードも不要で、3分で完了します。'),
        ('登録時に割引コードを入れ忘れた場合は？',
         '後からでもプラン変更時にコードを入力できます。ただし、最初の登録時に入力するのが確実です。'),
        ('日本語で登録できますか？',
         'はい。サイト全体が日本語対応しています。登録画面も全て日本語です。'),
    ],
    'plan-hikaku.html': [
        ('初心者はどのプランから始めるべきですか？',
         'まずは無料プランで試して、本格的に使うならスタンダード月払いがおすすめです。割引コード「CJ9852」で30%OFFになります。'),
        ('アドバンスとVIPの違いは？',
         '主にAPIアクセスとデータ取得量の上限が異なります。月商100万円以下ならスタンダードで十分です。'),
        ('プランはいつでも変更できますか？',
         'はい。アップグレードもダウングレードもいつでも可能です。'),
    ],
    'oubei-yunyuu.html': [
        ('欧米輸入にセラースプライトは必要ですか？',
         'はい。仕入れ前に日本のAmazon市場での需要・競合・価格帯をデータで確認できるので、仕入れの失敗を大幅に減らせます。'),
        ('海外Amazonのデータも見れますか？',
         'はい。アメリカ、ヨーロッパなど主要マーケットプレイスのデータに対応しています。'),
        ('欧米輸入で特に使う機能は？',
         '商品リサーチとキーワードリサーチが中心です。月間販売数の推定と競合数の確認が仕入れ判断に直結します。'),
    ],
    'fukugyou.html': [
        ('副業でAmazon物販を始めるのにツールは必要ですか？',
         '必須ではありませんが、限られた時間で成果を出すにはデータに基づく判断が不可欠です。感覚だけの仕入れはリスクが高くなります。'),
        ('月にどれくらいの時間が必要ですか？',
         'セラースプライトを使えば、リサーチは週1〜2時間で十分です。本業の合間でも無理なく続けられます。'),
        ('初期費用はどれくらいかかりますか？',
         'セラースプライトはスタンダード月払い¥13,998（割引コードで約¥9,799）から始められます。仕入れ資金を含めても数万円から可能です。'),
    ],
}


def build_faq_html(questions):
    """Build the full faq-section HTML block."""
    items = []
    for i, (q, a) in enumerate(questions):
        is_first = (i == 0)
        open_cls = ' open' if is_first else ''
        expanded = 'true' if is_first else 'false'
        items.append(
            f'      <div class="faq-item{open_cls}">\n'
            f'        <button class="faq-q" onclick="toggleFaq(this)" aria-expanded="{expanded}">\n'
            f'          <span>{q}</span>\n'
            f'          <span class="faq-icon">＋</span>\n'
            f'        </button>\n'
            f'        <div class="faq-a"><p class="faq-a-inner">{a}</p></div>\n'
            f'      </div>'
        )
    items_str = '\n'.join(items)
    return (
        '<div class="faq-section">\n'
        '  <div class="faq-inner">\n'
        '    <h2>よくある質問</h2>\n'
        '    <div class="faq-list">\n'
        f'{items_str}\n'
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
    )


def build_faqpage_jsonld(questions):
    """Build FAQPage JSON-LD script block."""
    entities = []
    for q, a in questions:
        entities.append(
            '    {\n'
            '      "@type": "Question",\n'
            f'      "name": "{q}",\n'
            '      "acceptedAnswer": {\n'
            '        "@type": "Answer",\n'
            f'        "text": "{a}"\n'
            '      }\n'
            '    }'
        )
    entities_str = ',\n'.join(entities)
    return (
        '<script type="application/ld+json">\n'
        '{\n'
        '  "@context": "https://schema.org",\n'
        '  "@type": "FAQPage",\n'
        '  "mainEntity": [\n'
        f'{entities_str}\n'
        '  ]\n'
        '}\n'
        '</script>'
    )


def process_file(filename, questions):
    path = os.path.join(BASE, filename)
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()

    original = html
    actions = []

    # ── 1. CSS ──────────────────────────────────────────────────────────────
    if '.faq-section' not in html:
        # For index.html which has different .faq-* CSS, we still need .faq-section
        # Find insertion point: before /* QUOTE SECTION */ or before first @media
        if '/* QUOTE SECTION */' in html:
            html = html.replace('/* QUOTE SECTION */', FAQ_CSS + '/* QUOTE SECTION */', 1)
            actions.append('CSS injected before /* QUOTE SECTION */')
        elif '@media' in html:
            idx = html.index('@media')
            html = html[:idx] + FAQ_CSS + '\n' + html[idx:]
            actions.append('CSS injected before first @media')
        else:
            actions.append('WARNING: CSS injection point not found')
    else:
        actions.append('CSS already present - skipped')

    # ── 2. FAQ HTML block ───────────────────────────────────────────────────
    faq_html = build_faq_html(questions)

    if filename == 'index.html':
        # index.html has existing FAQ section with different structure.
        # Replace the old <!-- FAQ --> block up to the closing div.
        # The old FAQ is wrapped in: <div class="section bg-white">...<div class="faq-list">...</div></div></div>
        # Let's replace the entire <!-- FAQ --> ... </div>\n</div> block
        old_faq_pattern = r'<!-- FAQ -->\s*<div class="section bg-white">.*?</div>\s*</div>\s*</div>'
        m = re.search(old_faq_pattern, html, re.DOTALL)
        if m:
            html = html[:m.start()] + faq_html + html[m.end():]
            actions.append('Old FAQ section replaced with new faq-section')
        else:
            # Fallback: insert before quote-section
            if '<div class="quote-section">' in html:
                html = html.replace('<div class="quote-section">', faq_html + '<div class="quote-section">', 1)
                actions.append('FAQ HTML inserted before quote-section (fallback)')
            else:
                actions.append('WARNING: FAQ insertion point not found for index.html')
    else:
        if 'class="faq-section"' not in html:
            if '<div class="quote-section">' in html:
                html = html.replace('<div class="quote-section">', faq_html + '<div class="quote-section">', 1)
                actions.append('FAQ HTML inserted before quote-section')
            else:
                actions.append('WARNING: quote-section not found')
        else:
            actions.append('faq-section already present - skipped')

    # ── 3. JS toggleFaq ─────────────────────────────────────────────────────
    if 'toggleFaq' not in html:
        # Add to existing script block that contains copyCode
        if 'function copyCode' in html:
            html = html.replace('function copyCode', FAQ_JS + '\nfunction copyCode', 1)
            actions.append('toggleFaq JS injected before copyCode')
        else:
            # Append before </script> in last script block
            last_script_end = html.rfind('</script>')
            if last_script_end != -1:
                html = html[:last_script_end] + FAQ_JS + '\n</script>' + html[last_script_end+9:]
                actions.append('toggleFaq JS appended to last script block')
            else:
                actions.append('WARNING: no script block found for JS injection')
    else:
        actions.append('toggleFaq already present - skipped')

    # ── 4. JSON-LD FAQPage ──────────────────────────────────────────────────
    if filename == 'index.html':
        # index.html has a combined JSON-LD with Article + FAQPage.
        # The FAQPage is already there but with old Q&As. Replace the entire FAQPage object within the array.
        # The structure is: [{"@type":"Article",...},{"@type":"FAQPage","mainEntity":[...]}]
        # Replace the FAQPage part
        faqpage_pattern = r'\{\s*"@type":\s*"FAQPage"[^}]*"mainEntity":\s*\[.*?\]\s*\}'
        new_faqpage = (
            '{\n'
            '      "@type": "FAQPage",\n'
            '      "mainEntity": [\n'
        )
        entities = []
        for q, a in questions:
            # Escape quotes in the text
            q_esc = q.replace('"', '\\"')
            a_esc = a.replace('"', '\\"')
            entities.append(
                '        {\n'
                '          "@type": "Question",\n'
                f'          "name": "{q_esc}",\n'
                '          "acceptedAnswer": {\n'
                '            "@type": "Answer",\n'
                f'            "text": "{a_esc}"\n'
                '          }\n'
                '        }'
            )
        new_faqpage += ',\n'.join(entities) + '\n      ]\n    }'
        m = re.search(faqpage_pattern, html, re.DOTALL)
        if m:
            html = html[:m.start()] + new_faqpage + html[m.end():]
            actions.append('FAQPage JSON-LD updated in existing combined script')
        else:
            actions.append('WARNING: FAQPage pattern not found in index.html JSON-LD')
    else:
        if 'FAQPage' not in html:
            # Build new FAQPage JSON-LD and insert after last </script> in head
            # Find the last </script> before </head>
            head_end = html.index('</head>')
            last_script_before_head = html.rfind('</script>', 0, head_end)
            if last_script_before_head != -1:
                insert_pos = last_script_before_head + len('</script>')
                jsonld = build_faqpage_jsonld(questions)
                html = html[:insert_pos] + '\n' + jsonld + html[insert_pos:]
                actions.append('FAQPage JSON-LD inserted after last head script')
            else:
                actions.append('WARNING: no head script block found for JSON-LD')
        else:
            actions.append('FAQPage JSON-LD already present - skipped')

    # ── 5. Write if changed ─────────────────────────────────────────────────
    if html != original:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f'[OK] {filename}')
    else:
        print(f'[NO CHANGE] {filename}')

    for a in actions:
        print(f'     - {a}')
    print()


def main():
    for filename, questions in FAQ_DATA.items():
        process_file(filename, questions)
    print('Done.')


if __name__ == '__main__':
    main()
