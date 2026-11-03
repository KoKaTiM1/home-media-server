package torr

import (
 "context"
 "io"
 "net/http"
 "net/http/httptest"
 "testing"
 "time"
 "server/settings"
)

type blockedStreamReader struct { entered chan struct{} }
func (r *blockedStreamReader) Read(p []byte) (int,error) { panic("uncancellable read used") }
func (r *blockedStreamReader) Seek(offset int64,whence int) (int64,error) { return offset,nil }
func (r *blockedStreamReader) ReadContext(ctx context.Context,p []byte) (int,error) {
 select { case r.entered<-struct{}{}: default: }
 <-ctx.Done()
 return 0,ctx.Err()
}
func TestStreamCancelHTTPDisconnectReleasesBlockedReadOnly(t *testing.T) {
 r:=&blockedStreamReader{make(chan struct{},1)}
 otherCtx,otherCancel:=context.WithCancel(context.Background()); defer otherCancel()
 finished:=make(chan struct{})
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,req *http.Request) {
  defer close(finished)
  _,_=newContextStreamReader(req.Context(),r).Read(make([]byte,1))
 }))
 defer server.Close()
 ctx,cancel:=context.WithCancel(context.Background());defer cancel()
 req,_:=http.NewRequestWithContext(ctx,"GET",server.URL,nil)
 returned:=make(chan struct{})
 go func(){defer close(returned);response,_:=server.Client().Do(req);if response!=nil {response.Body.Close()}}()
 select {case <-r.entered:case <-time.After(time.Second):t.Fatal("reader never entered")}
 cancel()
 select {case <-finished:case <-time.After(time.Second):t.Fatal("disconnected request retained blocked read")}
 <-returned
 select {case <-otherCtx.Done():t.Fatal("another request was cancelled");default:}
}
func TestStreamCancelConcurrentReadAndCancel(t *testing.T) {
 for i:=0;i<100;i++ {
  ctx,cancel:=context.WithCancel(context.Background())
  reader:=newContextStreamReader(ctx,&blockedStreamReader{make(chan struct{},1)})
  go cancel()
  if _,err:=reader.Read(make([]byte,1));err!=context.Canceled {t.Fatalf("unexpected read error: %v",err)}
  if pos,err:=reader.Seek(42,io.SeekStart);err!=nil||pos!=42 {t.Fatal("seek was not preserved")}
 }
}

// Exercise the real Stream handler and its cache reader lifecycle, using the
// pinned upstream test's metadata-only torrent. No peer supplies its pieces.
func TestStreamCancelRealHandlerReleasesCacheReader(t *testing.T) {
	oldPath, oldSets := settings.Path, settings.BTsets
	settings.Path = t.TempDir()
	if err := settings.InitSets(false, false); err != nil { t.Fatal(err) }
	t.Cleanup(func() {
		settings.NewTDB().CloseDB()
		settings.Path, settings.BTsets = oldPath, oldSets
	})
	_, tor := newSeasonTorrent(t)
	other := tor.NewReader(tor.Files()[1])
	defer tor.CloseReader(other)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest(http.MethodGet, "/stream?play&index=1", nil).WithContext(ctx)
	req.Header.Set("Range", "bytes=0-99")
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		_ = tor.Stream(1, req, httptest.NewRecorder())
	}()
	until := time.Now().Add(2*time.Second)
	for tor.GetCache().Readers() < 2 && time.Now().Before(until) {
		time.Sleep(10*time.Millisecond)
	}
	if tor.GetCache().Readers() != 2 { t.Fatal("stream reader was not registered") }
	cancel()
	select {
	case <-finished:
	case <-time.After(2*time.Second): t.Fatal("real handler retained a disconnected blocked reader")
	}
	if got:=tor.GetCache().Readers(); got!=1 {
		t.Fatalf("expected only the independent reader to remain, got %d",got)
	}
}
