"""Draws app.ico (a document with sound waves) so no binary needs to be stored in git."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([28, 16, 168, 240], radius=18, fill="#ffffff", outline="#c2185b", width=10)
for i, y in enumerate((64, 100, 136, 172)):
    d.rounded_rectangle([52, y, 144 - (i % 2) * 28, y + 14], radius=7, fill="#f9a825")
for r, w in ((40, 12), (72, 12), (104, 12)):
    d.arc([168 - r, 128 - r, 168 + r, 128 + r], start=-50, end=50, fill="#c2185b", width=w)
img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("app.ico written")
