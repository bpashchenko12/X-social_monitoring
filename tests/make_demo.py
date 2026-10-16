"""Generate clearly-labelled SYNTHETIC demo data into docs/demo-data (open dashboard with ?data=demo-data).
No API calls, no credits. Run: python3 -m tests.make_demo
"""
import math
import os
import random
import shutil
from datetime import datetime, timedelta, timezone

from pipeline import store
from pipeline.common import ROOT, iso, load_config

OUT = os.path.join(ROOT, "docs", "demo-data")

# (japanese, english, sentiment, promo)
BANK = {
    "campaigns": [
        ("XMの入金ボーナス100%、今回も申請して無事反映された。ありがたい", "Applied for XM's 100% deposit bonus again and it was credited without problems. Grateful.", "Positive", False),
        ("【XMTrading】新規口座開設で13,000円のボーナス！紹介コード[DEMO1] #PR", "[XMTrading] 13,000 JPY bonus on new account opening! Referral code [DEMO1] #PR", "Neutral", True),
        ("XMPがなかなか貯まらない…キャッシュバック率も下がった気がする", "XMP is hard to accumulate... and the cashback rate feels like it went down too.", "Negative", False),
        ("XMのプロモーション、条件が複雑すぎて結局よく分からない", "XM's promotion terms are so complicated that I still don't really get them.", "Negative", False),
        ("XMTradingのロイヤルティプログラムでXMPを現金化。出金も早かった", "Cashed out XMP via XMTrading's loyalty program. Withdrawal was fast too.", "Positive", False),
        ("XM秋のキャンペーン開始。最大500ドルのボーナス、期間は10/30まで", "XM autumn campaign started. Up to 500 USD bonus, until 10/30.", "Neutral", False),
    ],
    "competitions": [
        ("XMのトレードバトル参加中。順位は今のところ12位", "Taking part in XM's trade battle. Currently ranked 12th.", "Neutral", False),
        ("XMTradingのコンテストで賞金もらった！参加して良かった", "Won prize money in the XMTrading contest! Glad I joined.", "Positive", False),
        ("XM大会のランキング、明らかにロットを盛ってる人が上位でやる気なくす", "In the XM tournament ranking, people clearly inflating lots are on top. Lost my motivation.", "Negative", False),
        ("XMのデモコンテスト、来月開催だって。エントリー受付開始", "XM's demo contest is next month. Entries are open.", "Neutral", False),
        ("XMコンペは参加無料なのに賞品が豪華すぎる", "The XM competition is free to enter yet the prizes are too generous.", "Positive", False),
    ],
    "environment": [
        ("XMのスプレッドが指標前に大きく広がった。ゴールドはやっぱり厳しい", "XM's spread widened a lot before the data release. Gold is tough after all.", "Negative", False),
        ("XMTradingは約定力が高くて滑らない。スキャルでも使いやすい", "XMTrading has strong execution with little slippage. Easy to use even for scalping.", "Positive", False),
        ("XMのサーバーが落ちてる？MT5にログインできない", "Is XM's server down? Can't log in to MT5.", "Negative", False),
        ("XMのゼロカットがあるから高レバでも安心して取引できる", "Thanks to XM's negative balance protection I can trade with high leverage without worry.", "Positive", False),
        ("XMのコピートレーディング始めてみた。MT4から設定できる", "Tried starting XM copy trading. It can be set up from MT4.", "Neutral", False),
        ("XMのアプリ、アップデート後に挙動が重い", "XM's app feels sluggish after the update.", "Negative", False),
    ],
    "deposit_withdrawal": [
        ("XMの出金、申請から数時間で着金した。早い", "XM withdrawal arrived within hours of the request. Fast.", "Positive", False),
        ("XMに銀行送金で入金したけど反映されない。サポートに連絡中", "Deposited to XM by bank transfer but it hasn't been credited. Contacting support.", "Negative", False),
        ("XMのクレジットカード出金、手数料かからないのは助かる", "No fee on XM credit card withdrawals is helpful.", "Positive", False),
        ("XMTradingの入金方法まとめ。国内銀行、クレカ、bitwallet", "Summary of XMTrading deposit methods: domestic bank, credit card, bitwallet.", "Neutral", False),
        ("XM出金が3営業日経っても着金しない。さすがに遅い", "XM withdrawal still hasn't arrived after 3 business days. That's too slow.", "Negative", False),
    ],
    "account_types": [
        ("XMのKIWAMI極口座、スプレッド狭くてスキャルに向いてる", "XM's KIWAMI account has narrow spreads, suited to scalping.", "Positive", False),
        ("XMのスタンダード口座とゼロ口座、どっちがいい？違いを比較", "XM Standard vs Zero account - which is better? A comparison.", "Neutral", False),
        ("XMのマイクロ口座で少額から始めてみた。ロット管理しやすい", "Started with a small amount on an XM Micro account. Easy to manage lot sizes.", "Positive", False),
        ("XMの口座開設、本人確認が通るまで2日かかった", "XM account opening took 2 days until ID verification passed.", "Neutral", False),
        ("XMの追加口座を作ろうとしたら上限で作れなかった", "Tried to create an additional XM account but hit the limit.", "Negative", False),
    ],
    "macro": [
        ("XMTradingは日本人トレーダーに一番人気の海外FX業者だと思う", "I think XMTrading is the most popular overseas FX broker among Japanese traders.", "Positive", False),
        ("XMで今月プラス収支。コツコツ積み上げ", "In profit this month on XM. Building up steadily.", "Positive", False),
        ("XMの口コミ、良い悪いが極端に分かれてる印象", "Reviews of XM seem to be extremely split between good and bad.", "Neutral", False),
        ("XMで大損した。ハイレバは本当に気をつけて", "Took a big loss on XM. Be really careful with high leverage.", "Negative", False),
    ],
}
SUFFIX = ["", "", " #XMTrading", " #海外FX", "\n#XM", " 皆さんはどうですか？"]
SUFFIX_EN = ["", "", " #XMTrading", " #OverseasFX", "\n#XM", " How about you?"]


def main(seed=7):
    rnd = random.Random(seed)
    cfg = load_config()
    base = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    handles = [f"demo_trader_{i:02d}" for i in range(1, 29)]
    names = [f"Demo Trader {i}" for i in range(1, 29)]
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    posts, pid = [], 2000000000000000000
    for topic, bank in BANK.items():
        for _ in range(100):
            jp, en, sent, promo = rnd.choice(bank)
            s = rnd.randrange(len(SUFFIX))
            hi = rnd.random() < 0.12
            likes = int(math.exp(rnd.uniform(math.log(11), math.log(900 if not hi else 12000))))
            imp = int(likes * rnd.uniform(18, 70))
            when = base - timedelta(minutes=int(rnd.expovariate(1 / 4000)) % (60 * 24 * 21))
            pid += rnd.randrange(1, 10 ** 9)
            h = rnd.randrange(len(handles))
            topics = [topic] if topic == "macro" else ([topic, "macro"] if rnd.random() < 0.55 else [topic])
            posts.append({
                "id": str(pid), "created_at": iso(when), "account_name": names[h], "account_handle": handles[h],
                "account_followers": rnd.randrange(50, 40000), "lang": "ja",
                "content_original": jp + SUFFIX[s], "content_en": en + SUFFIX_EN[s],
                "impressions": imp if rnd.random() > 0.03 else None, "likes": likes,
                "retweets": int(likes * rnd.uniform(.02, .25)), "replies": int(likes * rnd.uniform(.01, .1)),
                "quotes": int(likes * rnd.uniform(0, .05)), "bookmarks": int(likes * rnd.uniform(.02, .3)),
                "url_clicks": None, "url": f"https://x.com/{handles[h]}/status/{pid}", "image_url": None,
                "topics": topics, "sentiment": sent if rnd.random() > 0.06 else None, "relevant": True,
                "exclude_reason": None, "is_promo": promo, "first_seen": iso(base), "last_seen": iso(base),
                "metrics_updated_at": iso(base), "enriched_at": iso(base), "enriched_by": "demo",
            })
    weeks = os.path.join(OUT, "weeks")
    store.upsert_posts(posts, weeks)
    m = store.rebuild_manifest(cfg["topics"], weeks, OUT)
    print(f"demo posts: {len(posts)}; weeks: {[w['week'] for w in m['weeks']]}; topics: {[(t['id'], t['posts']) for t in m['topics']]}")


if __name__ == "__main__":
    main()
