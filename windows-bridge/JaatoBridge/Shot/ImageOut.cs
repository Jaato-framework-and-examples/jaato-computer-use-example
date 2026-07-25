using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Formats.Png;
using SixLabors.ImageSharp.Formats.Webp;
using SixLabors.ImageSharp.PixelFormats;
using SixLabors.ImageSharp.Processing;
using Rectangle = SixLabors.ImageSharp.Rectangle; // disambiguate from System.Drawing (pulled in by WinForms)

namespace JaatoBridge.Shot;

/// <summary>
/// §7/§12 image finalisation: redaction is done in-buffer BEFORE this (so masked pixels never reach the
/// encoder), then crop → downsample to the model's input resolution (the biggest bandwidth lever) → encode.
/// </summary>
public static class ImageOut
{
    public sealed record Encoded(byte[] Bytes, int Width, int Height, string Format);

    public static Encoded Encode(byte[] bgra, int w, int h, int[]? crop, int maxDimension, string format)
    {
        using var img = Image.LoadPixelData<Bgra32>(bgra, w, h);

        if (crop is { Length: 4 })
        {
            var r = Clamp(crop, img.Width, img.Height);
            if (r.Width > 0 && r.Height > 0) img.Mutate(x => x.Crop(r));
        }

        if (maxDimension > 0 && Math.Max(img.Width, img.Height) > maxDimension)
        {
            double s = (double)maxDimension / Math.Max(img.Width, img.Height);
            img.Mutate(x => x.Resize(Math.Max(1, (int)(img.Width * s)), Math.Max(1, (int)(img.Height * s))));
        }

        using var ms = new MemoryStream();
        string fmt = format?.ToLowerInvariant() ?? "png";
        if (fmt == "webp") img.Save(ms, new WebpEncoder { Quality = 80 });
        else { fmt = "png"; img.Save(ms, new PngEncoder()); }
        return new Encoded(ms.ToArray(), img.Width, img.Height, fmt);
    }

    static Rectangle Clamp(int[] ltrb, int w, int h)
    {
        int l = Math.Clamp(ltrb[0], 0, w), t = Math.Clamp(ltrb[1], 0, h);
        int r = Math.Clamp(ltrb[2], 0, w), b = Math.Clamp(ltrb[3], 0, h);
        return new Rectangle(l, t, Math.Max(0, r - l), Math.Max(0, b - t));
    }
}
