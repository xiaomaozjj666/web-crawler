from lxml import html

s = '<html><body><div id="product-title">旧版标题</div></body></html>'
# 直接传 str（修复方案）
doc = html.fromstring(s)
print("str direct:", repr(doc.cssselect("#product-title")[0].text))
# 先 encode 成 bytes（现状路径）
doc2 = html.fromstring(s.encode("utf-8"))
print("bytes path:", repr(doc2.cssselect("#product-title")[0].text))
