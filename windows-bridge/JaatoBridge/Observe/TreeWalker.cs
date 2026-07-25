using System.Diagnostics;
using UIAutomationClient;

namespace JaatoBridge.Observe;

/// <summary>
/// Turns a window's UIA element into a pruned NodeSnap[] with parent links and a ref→RuntimeId table.
/// One bulk <c>BuildUpdatedCache(subtree)</c> materialises the whole window in-proc; the recursive walk
/// then reads only <c>Cached*</c> and applies the §8 prune, collapsing dropped containers by promoting
/// their kept descendants to the nearest kept ancestor.
/// </summary>
public sealed class TreeWalker
{
    readonly UiaSession _uia;
    public TreeWalker(UiaSession uia) => _uia = uia;

    public sealed class Result
    {
        public List<NodeSnap> Nodes { get; } = new();
        public Dictionary<int, int[]> RefTable { get; } = new();  // ref → RuntimeId
        public double WalkMs { get; init; }
    }

    public Result Walk(IUIAutomationElement windowElement)
    {
        var cr = _uia.BuildTreeCacheRequest();
        var sw = Stopwatch.StartNew();
        IUIAutomationElement root = windowElement.BuildUpdatedCache(cr);
        sw.Stop();

        var result = new Result { WalkMs = sw.Elapsed.TotalMilliseconds };
        int nextRef = 1;
        Descend(root, parentRef: null);
        return result;

        void Descend(IUIAutomationElement e, int? parentRef)
        {
            var props = Pruner.Read(e);
            int? childParent = parentRef;
            if (Pruner.Keep(props))
            {
                int refId = nextRef++;
                result.Nodes.Add(Pruner.ToSnap(refId, props, parentRef));
                if (props.RuntimeId.Length > 0) result.RefTable[refId] = props.RuntimeId;
                childParent = refId; // kept node becomes the parent for its kept descendants
            }
            // else: dropped container — its kept descendants attach to our own parent (collapse chain)

            IUIAutomationElementArray kids;
            try { kids = e.GetCachedChildren(); } catch { return; }
            if (kids is null) return;
            int n = kids.Length;
            for (int i = 0; i < n; i++)
            {
                IUIAutomationElement c;
                try { c = kids.GetElement(i); } catch { continue; }
                Descend(c, childParent);
            }
        }
    }
}
