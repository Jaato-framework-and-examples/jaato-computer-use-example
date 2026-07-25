using System.Diagnostics;
using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using WinRT;

namespace JaatoBridge.Shot;

/// <summary>
/// §7 screenshot source: Windows.Graphics.Capture (per-window), IsBorderRequired=false and
/// IsCursorCaptureEnabled=false (§12.3). The GPU frame is copied to a CPU-readable staging texture via
/// Vortice/D3D11 and returned as raw BGRA. Redaction / crop / downsample / encode happen in ImageOut.
/// </summary>
public sealed class ScreenCapturer : IDisposable
{
    readonly ID3D11Device _d3d;
    readonly ID3D11DeviceContext _ctx;
    readonly IDirect3DDevice _winrtDevice;

    static readonly Guid IID_ID3D11Texture2D = new("6f15aaf2-d208-4e89-9ab4-489535d34f9c");
    static readonly Guid IID_GraphicsCaptureItem = new("79C3F95B-31F7-4EC2-A464-632EF5D30760");

    public ScreenCapturer()
    {
        D3D11.D3D11CreateDevice(null, DriverType.Hardware, DeviceCreationFlags.BgraSupport,
            new[] { FeatureLevel.Level_11_1, FeatureLevel.Level_11_0 }, out _d3d!, out _ctx!);
        using var dxgi = _d3d.QueryInterface<IDXGIDevice>();
        int hr = CreateDirect3D11DeviceFromDXGIDevice(dxgi.NativePointer, out IntPtr graphicsDevicePtr);
        if (hr != 0) throw new InvalidOperationException($"CreateDirect3D11DeviceFromDXGIDevice hr=0x{hr:X8}");
        _winrtDevice = MarshalInterface<IDirect3DDevice>.FromAbi(graphicsDevicePtr);
        Marshal.Release(graphicsDevicePtr);
    }

    public static bool Supported()
    {
        try { return GraphicsCaptureSession.IsSupported(); } catch { return false; }
    }

    public sealed record Frame(byte[] Bgra, int Width, int Height);

    /// <summary>Capture one frame of the given window. Returns raw BGRA top-down rows.</summary>
    public Frame CaptureWindow(IntPtr hwnd, int timeoutMs = 2000) => Capture(CreateItemForWindow(hwnd), timeoutMs);

    /// <summary>
    /// Capture the whole primary monitor. Used as the fallback when a per-window capture can't produce a
    /// frame — e.g. the Start menu / Search (SearchHost) surface, which is DWM-cloaked and never yields a
    /// WGC frame via CreateForWindow, so a naive per-window capture times out. A monitor capture still shows
    /// the Start overlay, which is exactly what the model needs to see.
    /// </summary>
    public Frame CaptureMonitor(int timeoutMs = 2000)
    {
        var hmon = MonitorFromWindow(IntPtr.Zero, MONITOR_DEFAULTTOPRIMARY);
        return Capture(CreateItemForMonitor(hmon), timeoutMs);
    }

    Frame Capture(GraphicsCaptureItem item, int timeoutMs)
    {
        var size = item.Size;
        using var pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            _winrtDevice, DirectXPixelFormat.B8G8R8A8UIntNormalized, 1, size);
        using var session = pool.CreateCaptureSession(item);
        TrySet(() => session.IsCursorCaptureEnabled = false);
        TrySet(() => session.IsBorderRequired = false);
        session.StartCapture();

        Direct3D11CaptureFrame? frame = null;
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs && frame is null)
        {
            frame = pool.TryGetNextFrame();
            if (frame is null) Thread.Sleep(8);
        }
        if (frame is null) throw new TimeoutException("no WGC frame arrived");

        using (frame)
        {
            var access = frame.Surface.As<IDirect3DDxgiInterfaceAccess>();
            Guid texIid = IID_ID3D11Texture2D;
            IntPtr texPtr = access.GetInterface(ref texIid);
            using var tex = new ID3D11Texture2D(texPtr);
            var desc = tex.Description;

            var stagingDesc = desc;
            stagingDesc.Usage = ResourceUsage.Staging;
            stagingDesc.CPUAccessFlags = CpuAccessFlags.Read;
            stagingDesc.BindFlags = BindFlags.None;
            stagingDesc.MiscFlags = ResourceOptionFlags.None;
            using var staging = _d3d.CreateTexture2D(stagingDesc);

            _ctx.CopyResource(staging, tex);
            var map = _ctx.Map(staging, 0, MapMode.Read, Vortice.Direct3D11.MapFlags.None);
            try
            {
                int w = (int)desc.Width, h = (int)desc.Height;
                var bgra = new byte[w * h * 4];
                int rowPitch = (int)map.RowPitch;
                for (int y = 0; y < h; y++)
                    Marshal.Copy(IntPtr.Add(map.DataPointer, y * rowPitch), bgra, y * w * 4, w * 4);
                return new Frame(bgra, w, h);
            }
            finally { _ctx.Unmap(staging, 0); }
        }
    }

    static void TrySet(Action a) { try { a(); } catch { } }

    // ---- WGC item interop (§7) ----
    // IUnknown-derived interop interface (NOT IInspectable) — the correct declaration for both
    // vtable layout and CsWinRT AsInterface<T> marshaling.
    [ComImport, Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IGraphicsCaptureItemInterop
    {
        IntPtr CreateForWindow([In] IntPtr window, [In] ref Guid iid);
        IntPtr CreateForMonitor([In] IntPtr monitor, [In] ref Guid iid);
    }

    [ComImport, Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IDirect3DDxgiInterfaceAccess { IntPtr GetInterface([In] ref Guid iid); }

    GraphicsCaptureItem CreateItemForWindow(IntPtr hwnd)
    {
        var interop = ActivationFactory.Get("Windows.Graphics.Capture.GraphicsCaptureItem").AsInterface<IGraphicsCaptureItemInterop>();
        Guid iid = IID_GraphicsCaptureItem;
        IntPtr itemPtr = interop.CreateForWindow(hwnd, ref iid);
        var item = GraphicsCaptureItem.FromAbi(itemPtr);
        Marshal.Release(itemPtr);
        return item;
    }

    GraphicsCaptureItem CreateItemForMonitor(IntPtr hmon)
    {
        var interop = ActivationFactory.Get("Windows.Graphics.Capture.GraphicsCaptureItem").AsInterface<IGraphicsCaptureItemInterop>();
        Guid iid = IID_GraphicsCaptureItem;
        IntPtr itemPtr = interop.CreateForMonitor(hmon, ref iid);
        var item = GraphicsCaptureItem.FromAbi(itemPtr);
        Marshal.Release(itemPtr);
        return item;
    }

    const uint MONITOR_DEFAULTTOPRIMARY = 1;
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);

    [DllImport("d3d11.dll", ExactSpelling = true)]
    static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr dxgiDevice, out IntPtr graphicsDevice);

    public void Dispose() { _ctx.Dispose(); _d3d.Dispose(); }
}
