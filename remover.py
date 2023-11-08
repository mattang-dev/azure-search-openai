import fitz  # PyMuPDF

file='./data/DAMA-DMBOK2.pdf'
doc = fitz.open(file)

for page in doc:
    draft = page.search_for("Order 51916 by Michael Kleinhaus on August 21, 2023")
    for rect in draft:
        annot = page.add_redact_annot(rect)
        annot.set_colors({"fill": (1, 1, 1)})  # set fill color to white
        annot.set_info(info={"content": ""})  # no replacement text
        annot.update()
    page.apply_redactions()  # apply all redactions on this page

doc.save("new.pdf", garbage=4, deflate=True)