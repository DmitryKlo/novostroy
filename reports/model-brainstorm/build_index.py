"""Validate seven idea banks and build a standalone offline research index.

Usage: rtk proxy .runtime/dev-env/bin/python reports/model-brainstorm/build_index.py
       rtk proxy .runtime/dev-env/bin/python reports/model-brainstorm/build_index.py --check
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
from pathlib import Path
import re
import unicodedata


ROOT = Path(__file__).resolve().parent
AUTHORS = {
    "LC": ("luna_cache", "Luna cache"),
    "LL": ("luna_lod", "Luna LOD"),
    "RS": ("report_sources", "Report sources"),
    "SW": ("sol_web_cache", "Sol web/cache"),
    "SP": ("sol_map_pipeline", "Sol map pipeline"),
    "SR": ("sol_registry", "Sol registry"),
    "SM": ("sol_integration", "Sol-Max integration"),
}
IDEA_LINE = re.compile(r"^.{0,20}?(?P<id>(?P<author>LC|LL|RS|SW|SP|SR|SM)-Q(?P<question>[1-4])-(?P<number>\d{3}))(?P<tail>.*)$")


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = "".join(" " if unicodedata.category(char).startswith(("P", "S")) else char for char in value)
    return " ".join(value.split())


def read_ideas() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for code, (stem, display) in AUTHORS.items():
        source = ROOT / f"{stem}-ideas.md"
        found: list[dict[str, str]] = []
        for line in source.read_text(encoding="utf-8").splitlines():
            match = IDEA_LINE.match(line)
            if match is None:
                continue
            assert match["author"] == code, (source.name, match["id"])
            idea = match["tail"].lstrip(" .*—–:\t")
            assert idea, match["id"]
            found.append({"id": match["id"], "author": display, "question": f"Q{match['question']}",
                          "idea": idea, "source_file": source.name})
        expected = [f"{code}-Q{question}-{number:03}" for question in range(1, 5) for number in range(1, 101)]
        assert [item["id"] for item in found] == expected, (source.name, len(found))
        items.extend(found)
    assert len(items) == len({item["id"] for item in items}) == 2800
    return items


def duplicate_groups(items: list[dict[str, str]]) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for item in items:
        grouped.setdefault(normalize(item["idea"]), []).append(item["id"])
    return [(key, ids) for key, ids in sorted(grouped.items()) if len(ids) > 1]


def make_csv(items: list[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=("id", "author", "question", "idea", "source_file"), lineterminator="\n")
    writer.writeheader()
    writer.writerows(items)
    return output.getvalue()


def make_duplicate_csv(groups: list[tuple[str, list[str]]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("normalized_text", "ids"))
    writer.writerows((text, " ".join(ids)) for text, ids in groups)
    return output.getvalue()


def make_html(items: list[dict[str, str]]) -> str:
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    options = "".join(f'<option value="{code}">{html.escape(name)}</option>' for code, (_, name) in AUTHORS.items())
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>2800 кандидатов · модели новостроек</title>
<style>
:root {{ color-scheme: light; font-family: system-ui, sans-serif; background: #f4f2ed; color: #1c2827 }}
body {{ max-width: 1100px; margin: auto; padding: 1rem }}
h1 {{ line-height: 1.1 }} p {{ max-width: 80ch; line-height: 1.5 }}
a {{ color: #1d5a67 }} .links {{ display: flex; gap: .8rem; flex-wrap: wrap }}
.controls {{ display: flex; gap: .5rem; flex-wrap: wrap; margin: 1.5rem 0 }}
input, select, button {{ font: inherit; padding: .55rem .7rem; border: 1px solid #8c9996; border-radius: .4rem; background: white }}
input {{ flex: 1 1 18rem }} button {{ cursor: pointer }} button:disabled {{ opacity: .45; cursor: default }}
.item {{ padding: .85rem; margin: .55rem 0; background: white; border-left: .25rem solid #537e7a; border-radius: .2rem; line-height: 1.45 }}
.meta {{ color: #52625e; font-size: .9rem; margin-bottom: .35rem }}
.pager {{ display: flex; align-items: center; gap: .6rem; justify-content: space-between; margin: 1rem 0 }}
</style></head><body>
<h1>2800 кандидатов для 3D новостроек</h1>
<p><strong>Это идеи, не проверенные решения.</strong> Обнаруженная страница не означает полученный GLB, измеренную геометрию или право на публикацию. Текущая стадия и размеры будущего корпуса требуют отдельных источников. Расходы исследования: $0; общий потолок возможных внешних экспериментов — $20.</p>
<nav class="links"><a href="CONCEPT.md">Концепт</a><a href="DEBATE.md">Дебаты</a><a href="SOURCE-MATRIX.md">Матрица источников</a><a href="BUDGET.md">Бюджет</a><a href="VALIDATION.md">Проверка</a><a href="ideas.csv">CSV</a></nav>
<div class="controls"><input id="query" type="search" placeholder="Поиск по тексту или ID" aria-label="Поиск">
<select id="author" aria-label="Автор"><option value="">Все авторы</option>{options}</select>
<select id="question" aria-label="Вопрос"><option value="">Все вопросы</option><option>Q1</option><option>Q2</option><option>Q3</option><option>Q4</option></select>
<select id="size" aria-label="Строк на странице"><option>50</option><option>100</option></select></div>
<p id="count" role="status"></p><div id="items"></div>
<div class="pager"><button id="prev" type="button">Назад</button><span id="page"></span><button id="next" type="button">Вперёд</button></div>
<script id="data" type="application/json">{payload}</script>
<script>
const all = JSON.parse(document.getElementById('data').textContent);
const query = document.getElementById('query'), author = document.getElementById('author');
const question = document.getElementById('question'), size = document.getElementById('size');
const list = document.getElementById('items'), count = document.getElementById('count');
const pageLabel = document.getElementById('page'), previous = document.getElementById('prev'), next = document.getElementById('next');
let page = 0;
function render() {{
  const term = query.value.trim().toLocaleLowerCase();
  const selected = all.filter(row => (!author.value || row.id.startsWith(author.value + '-'))
    && (!question.value || row.question === question.value)
    && (!term || (row.id + ' ' + row.idea).toLocaleLowerCase().includes(term)));
  const perPage = Number(size.value), pages = Math.max(1, Math.ceil(selected.length / perPage));
  page = Math.min(page, pages - 1); count.textContent = `Найдено ${{selected.length}} из ${{all.length}} кандидатов`;
  pageLabel.textContent = `Страница ${{page + 1}} из ${{pages}}`;
  previous.disabled = page === 0; next.disabled = page >= pages - 1;
  list.replaceChildren();
  for (const row of selected.slice(page * perPage, (page + 1) * perPage)) {{
    const card = document.createElement('article'); card.className = 'item';
    const meta = document.createElement('div'); meta.className = 'meta';
    const link = document.createElement('a'); link.href = row.source_file; link.textContent = row.id;
    meta.append(link, document.createTextNode(' · ' + row.author + ' · ' + row.question));
    const content = document.createElement('div'); content.textContent = row.idea;
    card.append(meta, content); list.append(card);
  }}
}}
for (const element of [query, author, question, size]) element.addEventListener('input', () => {{ page = 0; render() }});
previous.addEventListener('click', () => {{ page--; render(); scrollTo(0, 0) }});
next.addEventListener('click', () => {{ page++; render(); scrollTo(0, 0) }});
render();
</script></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate generated artifacts without writing")
    args = parser.parse_args()
    items = read_ideas()
    duplicates = duplicate_groups(items)
    artifacts = {"ideas.csv": make_csv(items), "EXACT-DUPLICATES.csv": make_duplicate_csv(duplicates),
                 "index.html": make_html(items)}
    for name, content in artifacts.items():
        path = ROOT / name
        if args.check:
            assert path.read_text(encoding="utf-8") == content, f"Stale generated artifact: {name}"
        else:
            path.write_text(content, encoding="utf-8")
    print(f"PASS: {len(items)} ideas, 100 per question per author, {len(duplicates)} normalized exact duplicate groups; "
          f"artifacts {'current' if args.check else 'written'}")


if __name__ == "__main__":
    main()
