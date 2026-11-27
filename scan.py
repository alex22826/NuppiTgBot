import sys
sys.path.insert(0, '.decoder_lib')
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import zxingcpp
for path in sorted(Path('approved_datamatrix').glob('*.jpg')):
    source = Image.open(path).convert('L')
    variants = []
    for angle in (0, 90, 180, 270):
        image = source.rotate(angle, expand=True)
        for scale in (1, 2, 3, 4):
            resized = image.resize((image.width*scale, image.height*scale), Image.Resampling.LANCZOS)
            variants += [resized, ImageOps.autocontrast(resized)]
            for cutoff in (90,110,130,150,170,190):
                variants.append(resized.point(lambda x, c=cutoff: 255 if x > c else 0))
    decoded = []
    for image in variants:
        for item in zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.DataMatrix, try_rotate=True, try_downscale=True):
            if item.text not in decoded:
                decoded.append(item.text)
    print(path.name, decoded)
