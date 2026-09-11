# -*- coding: utf-8 -*-
"""文本抽取：PDF（PyMuPDF，逐页）/ DOCX（段落 + 表格，简历常把姓名联系方式放表格）/ TXT。

保留 page_texts 列表：页偏移用于把 span 映射回页码（PDF 才有意义）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Extracted:
    text: str
    pages: int
    file_format: str
    extractor: str
    page_texts: list[str] = field(default_factory=list)


def extract(path: str) -> Extracted:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pymupdf

        doc = pymupdf.open(path)
        page_texts = [p.get_text() for p in doc]
        doc.close()
        text = "\n".join(page_texts)
        fmt, extractor = "pdf", f"pymupdf-{pymupdf.__doc__.split()[1] if pymupdf.__doc__ else 'pymupdf'}"
        return Extracted(text=text, pages=max(1, len(page_texts)), file_format=fmt, extractor=extractor,
                         page_texts=page_texts)

    if ext in (".docx", ".doc"):
        import docx

        d = docx.Document(path)
        parts: list[str] = [p.text for p in d.paragraphs]
        # 段落 + 表格都要抽（只读 paragraphs 会漏掉表格里的姓名/联系方式）
        seen_rows: set[str] = set()
        for t in d.tables:
            for row in t.rows:
                seen_cells: set[str] = set()
                for c in row.cells:
                    txt = c.text.strip()
                    if txt and txt not in seen_cells:
                        seen_cells.add(txt)
                        if txt not in seen_rows:
                            parts.append(txt)
                seen_rows |= seen_cells
        text = "\n".join(x for x in parts if x.strip())
        return Extracted(text=text, pages=1, file_format="docx", extractor="python-docx", page_texts=[text])

    if ext == ".doc":
        raise ValueError("旧版 .doc 需要先另存为 .docx 或走 OCR 兜底（v1 不支持）")

    text = open(path, encoding="utf-8", errors="replace").read()
    return Extracted(text=text, pages=1, file_format="txt", extractor="builtin", page_texts=[text])
