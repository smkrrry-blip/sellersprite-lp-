"""
AI翻訳モジュール
英語テキスト → タイ語（＋日本語）
Claude API優先、フォールバックでGoogle Translate無料版
"""
import re
import time
import logging
import requests
from typing import Optional
from config import ANTHROPIC_API_KEY, GOOGLE_TRANSLATE_API_KEY, LISTING_SETTINGS

logger = logging.getLogger(__name__)


class Translator:

    def __init__(self):
        self.use_claude = bool(ANTHROPIC_API_KEY)
        self.use_google = bool(GOOGLE_TRANSLATE_API_KEY)
        logger.info(f"翻訳エンジン: {'Claude' if self.use_claude else 'Google Translate (free)' if self.use_google else 'deep-translator (無料)'}")

    # ─── メインメソッド ───────────────────────────────────

    def translate_product(self, model: dict) -> dict:
        """
        商品データ全体をタイ語に翻訳
        title_en + description_en → title_th + description_th
        """
        title_en = model.get("title_en", "")
        desc_en = model.get("description_en", "")
        tags = model.get("tags", [])
        reviews = model.get("reviews", [])

        if self.use_claude:
            result = self._translate_with_claude(title_en, desc_en, tags, reviews)
        else:
            result = self._translate_with_free(title_en, desc_en, tags)

        model.update(result)
        return model

    # ─── Claude API 翻訳 ────────────────────────────────

    def _translate_with_claude(
        self, title: str, desc: str, tags: list, reviews: list
    ) -> dict:
        """Claude APIを使った高品質な商品説明生成・翻訳"""
        review_text = "\n".join(f"- {r}" for r in reviews[:3]) if reviews else "なし"
        tags_text = ", ".join(tags[:10]) if tags else ""

        prompt = f"""あなたはタイのShopeeで3Dプリント商品を販売するプロのコピーライターです。
以下の商品情報を基に、Shopeeタイ向けの魅力的な商品説明を作成してください。

【元の商品情報（英語）】
タイトル: {title}
説明: {desc[:500] if desc else 'なし'}
タグ: {tags_text}
レビュー例:
{review_text}

【出力してください】
以下のJSON形式で出力してください：

{{
  "title_th": "タイ語タイトル（60文字以内）",
  "description_th": "タイ語商品説明（500文字以内）。素材・用途・特徴を含める。CTAで締める。",
  "title_en_optimized": "英語最適化タイトル（Shopee SEO向け）",
  "keywords_th": ["タイ語キーワード1", "キーワード2", "キーワード3"]
}}

注意:
- タイ語は自然で購買意欲を高める表現を使う
- 3Dプリント商品であることを隠さない（素材: PLA樹脂）
- カスタムオーダー可能と記載してよい
- Shopeeのガイドラインに準拠する"""

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text
            # JSONを抽出
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                import json
                data = json.loads(match.group())
                return {
                    "title_th": data.get("title_th", title),
                    "description_th": data.get("description_th", desc),
                    "keywords_th": data.get("keywords_th", []),
                }
        except ImportError:
            logger.warning("anthropic パッケージ未インストール。pip install anthropic")
        except Exception as e:
            logger.error(f"Claude翻訳エラー: {e}")

        # フォールバック
        return self._translate_with_free(title, desc, tags)

    # ─── 無料翻訳 ──────────────────────────────────────

    def _translate_with_free(self, title: str, desc: str, tags: list) -> dict:
        """deep-translatorライブラリ（Google Translate無料版）を使用"""
        try:
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source="en", target="th")

            title_th = translator.translate(title) if title else ""
            # 長いテキストは分割して翻訳
            desc_th = self._translate_long_text(translator, desc) if desc else ""

            return {
                "title_th": title_th,
                "description_th": self._build_description_th(
                    title_th, desc_th, tags
                ),
                "keywords_th": [],
            }

        except ImportError:
            logger.warning("deep-translator未インストール。pip install deep-translator")
            return self._simple_template(title, desc)
        except Exception as e:
            logger.error(f"無料翻訳エラー: {e}")
            return self._simple_template(title, desc)

    def _translate_long_text(self, translator, text: str, chunk_size: int = 4500) -> str:
        """長いテキストをチャンクに分けて翻訳"""
        if len(text) <= chunk_size:
            try:
                return translator.translate(text)
            except Exception:
                return text

        chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
        translated_chunks = []
        for chunk in chunks:
            try:
                translated_chunks.append(translator.translate(chunk))
                time.sleep(0.5)
            except Exception:
                translated_chunks.append(chunk)
        return " ".join(translated_chunks)

    def _build_description_th(self, title_th: str, desc_th: str, tags: list) -> str:
        """タイ語商品説明を構造化"""
        desc_parts = []

        if desc_th:
            desc_parts.append(desc_th[:300])

        desc_parts.append("\n\n📦 วัสดุ: พลาสติก PLA คุณภาพสูง")
        desc_parts.append("🖨️ พิมพ์ด้วยเครื่อง 3D Printer Bambu Lab A1")
        desc_parts.append("✅ รับประกันคุณภาพทุกชิ้น")
        desc_parts.append("🎨 สามารถสั่งทำสีพิเศษได้ (ติดต่อทางแชต)")
        desc_parts.append("\n💬 สอบถามข้อมูลเพิ่มเติมได้เลยครับ/ค่ะ!")

        return "\n".join(desc_parts)

    def _simple_template(self, title_en: str, desc_en: str) -> dict:
        """翻訳失敗時のテンプレートフォールバック"""
        return {
            "title_th": f"ชิ้นงาน 3D Print - {title_en[:40]}",
            "description_th": (
                f"สินค้า 3D Printed คุณภาพสูง\n"
                f"วัสดุ: PLA\n"
                f"พิมพ์ด้วยเครื่อง Bambu Lab A1\n"
                f"✅ รับประกันคุณภาพ\n"
                f"💬 ติดต่อสอบถามได้เลย!"
            ),
            "keywords_th": [],
        }


class PriceCalculator:
    """価格計算（原価 → 販売価格）"""

    from config import (
        FILAMENT_COST_PER_GRAM_THB,
        PRINT_COST_PER_HOUR_THB,
        LISTING_SETTINGS,
    )

    @staticmethod
    def calculate(weight_g: float = 50, print_hours: float = 3) -> dict:
        from config import (
            FILAMENT_COST_PER_GRAM_THB,
            PRINT_COST_PER_HOUR_THB,
            LISTING_SETTINGS,
        )
        filament_cost = weight_g * FILAMENT_COST_PER_GRAM_THB
        print_cost = print_hours * PRINT_COST_PER_HOUR_THB
        packaging_cost = 15  # 梱包材 THB
        base_cost = filament_cost + print_cost + packaging_cost

        # Shopee手数料（約8〜10%）
        shopee_fee_rate = 0.09
        markup = LISTING_SETTINGS["markup_rate"]

        # 販売価格 = 原価 × マークアップ（手数料込み）
        sell_price = max(base_cost * markup, LISTING_SETTINGS["min_price_thb"])
        sell_price = round(sell_price / 5) * 5  # 5THB単位に丸める

        profit = sell_price * (1 - shopee_fee_rate) - base_cost
        profit_rate = profit / sell_price if sell_price > 0 else 0

        return {
            "cost_thb": round(base_cost, 1),
            "price_thb": sell_price,
            "profit_thb": round(profit, 1),
            "profit_rate_pct": round(profit_rate * 100, 1),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    t = Translator()
    result = t._simple_template("Cable Management Box", "Keep your desk organized")
    print("翻訳結果（テンプレート）:")
    print(f"  title_th: {result['title_th']}")
    print(f"  desc_th: {result['description_th'][:100]}...")

    price = PriceCalculator.calculate(weight_g=50, print_hours=3)
    print(f"\n価格計算（50g / 3時間）:")
    print(f"  原価: {price['cost_thb']} THB")
    print(f"  販売価格: {price['price_thb']} THB")
    print(f"  利益率: {price['profit_rate_pct']}%")
