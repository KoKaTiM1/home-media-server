using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.DynamicLibrary.Filters;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using Microsoft.Extensions.Logging.Abstractions;

public static class FirstOpenChecks
{
    public static async Task Run()
    {
        static void Check(bool value, string message) { if (!value) throw new Exception(message); }
        const string imdb = "tt00999991";
        var path = $"/catalogue/movies/{imdb}/movie.strm";
        Check(!Directory.Exists(Path.GetDirectoryName(path)), "First-open fixture must start absent");
        var manager = DispatchProxy.Create<ILibraryManager, FirstOpenLibraryProxy>();
        var store = (FirstOpenLibraryProxy)(object)manager;
        var id = manager.GetNewItemId(path, typeof(Movie));
        var userData = new UserItemDataDto { Key = "first-open-fixture", IsFavorite = true, PlaybackPositionTicks = 456000000, Played = false };
        var dto = new BaseItemDto { Id = id, Name = "First open", Type = BaseItemKind.Movie,
            ProductionYear = 2026, ProviderIds = new() { ["Imdb"] = imdb }, UserData = userData };
        var filter = new ItemLookupFilter(null!,null!,null!,null!,null!,manager,null!,null!,NullLogger<ItemLookupFilter>.Instance);
        var ensure = typeof(ItemLookupFilter).GetMethod("EnsureHomeCatalogueItem",BindingFlags.Instance|BindingFlags.NonPublic)!;
        void Register(BaseItemDto item) => ensure.Invoke(filter,new object[] { item });
        try
        {
            // The real registrar cannot create an indexed item before the source
            // lookup has materialized its file. This reproduces the old ordering.
            Register(dto);
            Check(manager.GetItemById(id) == null, "Absent STRM cannot already be a native movie");
            async Task Populate(BaseItemDto item, CancellationToken ct)
            {
                await Task.Yield();ct.ThrowIfCancellationRequested();
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllText(path,$"dynamiclibrary://movie/{imdb}\n");
            }
            await ItemLookupFilter.CompleteCatalogueDetailsAsync(dto,Populate,Register,CancellationToken.None);
            var native = manager.GetItemById(id);
            Check(native is Movie && native.Path == path && native.ParentId == store.Root.Id,
                "First details completion must leave the stable native Movie available to Play");
            Check(native!.Id == dto.Id && native.ProviderIds["Imdb"] == imdb,
                "Details and indexed movie retain one canonical identity");
            Check(ReferenceEquals(dto.UserData,userData) && dto.UserData.IsFavorite == true
                && dto.UserData.PlaybackPositionTicks == 456000000, "Registration preserves user state");
            await ItemLookupFilter.CompleteCatalogueDetailsAsync(dto,(_,_)=>Task.CompletedTask,Register,CancellationToken.None);
            Check(store.Creates == 1 && ReferenceEquals(native,manager.GetItemById(id)), "Repeated lookup is idempotent");
            var registered = false;
            using var stop = new CancellationTokenSource();stop.Cancel();
            try
            {
                await ItemLookupFilter.CompleteCatalogueDetailsAsync(dto,(_,_)=>Task.CompletedTask,
                    _=>registered=true,stop.Token);
                throw new Exception("Cancelled details must not complete registration");
            }
            catch (OperationCanceledException) { }
            Check(!registered,"Cancelled request does not register afterward");
            try
            {
                await ItemLookupFilter.CompleteCatalogueDetailsAsync(dto,(_,_)=>Task.FromException(new IOException("fixture")),
                    _=>registered=true,CancellationToken.None);
                throw new Exception("Failed source lookup must not complete registration");
            }
            catch (IOException) { }
            Check(!registered,"Failed source lookup cannot register a playable placeholder");
            Console.WriteLine("PASS: first-open absent-file materialization, native registration, stable ID, user state, idempotence and cancellation");
        }
        finally { Directory.Delete(Path.GetDirectoryName(path)!,true); }
    }
}

public class FirstOpenLibraryProxy : DispatchProxy
{
    public Folder Root { get; } = new() { Id = Guid.NewGuid(), Path = "/catalogue/movies", Name = "Movies" };
    public Dictionary<Guid,BaseItem> Items { get; } = new();
    public int Creates;
    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        args ??= Array.Empty<object>();
        if (method!.Name == "GetItemById") return Items.GetValueOrDefault((Guid)args[0]!);
        if (method.Name == "FindByPath") return args[0]?.ToString() == Root.Path ? Root : null;
        if (method.Name == "GetNewItemId") return new Guid(MD5.HashData(Encoding.UTF8.GetBytes(args[0]+":"+args[1])));
        if (method.Name == "CreateItem")
        {
            var item=(BaseItem)args[0]!;item.ParentId=(args[1] as BaseItem)?.Id ?? Guid.Empty;
            Items.Add(item.Id,item);Creates++;return null;
        }
        throw new NotSupportedException(method.Name);
    }
}
