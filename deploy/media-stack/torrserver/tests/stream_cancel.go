package torr

import (
 "context"
 "io"
)

// Keep seeks and TorrServer cache accounting on the normal reader while giving
// each blocking torrent read the HTTP request's cancellation context.
type streamContextReadSeeker interface {
 io.ReadSeeker
 ReadContext(context.Context, []byte) (int, error)
}
type contextStreamReader struct {
 streamContextReadSeeker
 ctx context.Context
}
func newContextStreamReader(ctx context.Context, source streamContextReadSeeker) *contextStreamReader {
 return &contextStreamReader{source,ctx}
}
func (r *contextStreamReader) Read(p []byte) (int,error) {
 return r.streamContextReadSeeker.ReadContext(r.ctx,p)
}
