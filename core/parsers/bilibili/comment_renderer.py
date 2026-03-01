from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


class BiliCommentRenderer:
    async def render_merged_comments(
        self,
        out_path: Path,
        comments: list[dict],
        video_title: str,
        video_cover: str | None,
    ):
        comments_html = ""
        for c in comments:
            uname = escape(c.get("uname", ""))
            avatar = c.get("avatar", "")
            message = escape(c.get("message", "")).replace("\n", "<br>")
            pic = c.get("pic")
            img_block = f'<div class="img-box"><img src="{pic}"></div>' if pic else ""

            comments_html += f"""
            <div class="card">
                <div class="user">
                    <img class="avatar" src="{avatar}">
                    <div class="name">{uname}</div>
                </div>
                <div class="text">{message}</div>
                {img_block}
            </div>
            """

        cover_block = f'<img class="v-cover" src="{video_cover}">' if video_cover else ""

        html = f"""
        <!doctype html>
        <html>
        <head>
          <link rel="preconnect" href="https://fonts.googleapis.com">
          <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
          <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+SC:wght@400;500;700;900&display=swap" rel="stylesheet">
          <style>
            body {{
              margin: 0;
              padding: 30px;
              width: 500px;
              font-family: 'Inter', 'Noto Sans SC', sans-serif;
              background: #f1f2f3;
            }}
            .header {{
              display: flex;
              align-items: center;
              margin-bottom: 24px;
            }}
            .v-cover {{
              width: 80px;
              height: 50px;
              border-radius: 6px;
              object-fit: cover;
              margin-right: 14px;
              box-shadow: 0 2px 8px rgba(0,0,0,0.1);
              flex-shrink: 0;
            }}
            .v-title {{
              font-size: 16px;
              font-weight: 700;
              color: #222;
              line-height: 1.4;
              display: -webkit-box;
              -webkit-line-clamp: 2;
              -webkit-box-orient: vertical;
              overflow: hidden;
            }}
            .card {{
              background: #fff;
              border-radius: 16px;
              padding: 20px;
              margin-bottom: 16px;
              box-shadow: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
            }}
            .user {{
              display: flex;
              align-items: center;
              margin-bottom: 12px;
            }}
            .avatar {{
              width: 40px;
              height: 40px;
              border-radius: 8px;
              margin-right: 12px;
            }}
            .name {{
              font-weight: 700;
              font-size: 15px;
              color: #333;
            }}
            .text {{
              font-size: 16px;
              line-height: 1.6;
              color: #222;
              white-space: pre-wrap;
              word-break: break-all;
            }}
            .img-box {{
              margin-top: 12px;
            }}
            .img-box img {{
              width: 100%;
              max-height: 450px;
              object-fit: cover;
              border-radius: 8px;
            }}
            .footer {{
              text-align: center;
              margin-top: 30px;
              color: #aaa;
              font-size: 12px;
              font-weight: 500;
              letter-spacing: 0.5px;
            }}
          </style>
        </head>
        <body>
          <div class="header">
            {cover_block}
            <div class="v-title">{escape(video_title)}</div>
          </div>

          {comments_html}

          <div class="footer">Menkelo/astrbot_plugin_r_parser</div>
        </body>
        </html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(
                viewport={"width": 560, "height": 10},
                device_scale_factor=2,
            )
            await page.set_content(html, wait_until="networkidle")

            height = await page.evaluate("document.body.scrollHeight")
            await page.set_viewport_size({"width": 560, "height": height})
            await page.wait_for_timeout(50)

            await page.screenshot(path=str(out_path), full_page=True)
            await browser.close()
