package settings

import (
	"encoding/json"
	"fmt"
	"github.com/anacrolix/torrent"
	"github.com/anacrolix/torrent/metainfo"
	"sync"
	"testing"
)

type indexFixture struct {
	rows              map[string][]byte
	gets, sets, lists int
}

func (d *indexFixture) CloseDB()                  {}
func (d *indexFixture) Get(x, n string) []byte    { d.gets++; return d.rows[n] }
func (d *indexFixture) Set(x, n string, b []byte) { d.sets++; d.rows[n] = append([]byte(nil), b...) }
func (d *indexFixture) List(x string) []string {
	d.lists++
	keys := []string{}
	for k := range d.rows {
		keys = append(keys, k)
	}
	return keys
}
func (d *indexFixture) Rem(x, n string) { delete(d.rows, n) }
func (d *indexFixture) Clear(x string)  { d.rows = map[string][]byte{} }
func fixtureTorrent(i int) *TorrentDB {
	h := metainfo.NewHashFromHex(fmt.Sprintf("%040x", i))
	return &TorrentDB{TorrentSpec: &torrent.TorrentSpec{InfoHash: h, InfoBytes: []byte("exact fixture metadata")}, Title: fmt.Sprintf("fixture %d", i)}
}
func setupIndex(t *testing.T) *indexFixture {
	old := tdb
	d := &indexFixture{rows: map[string][]byte{}}
	tdb = d
	t.Cleanup(func() { tdb = old })
	for i := 1; i <= 950; i++ {
		r := fixtureTorrent(i)
		b, _ := json.Marshal(r)
		d.rows[r.InfoHash.HexString()] = b
	}
	return d
}
func TestIndexedTorrentLookupAndSave(t *testing.T) {
	d := setupIndex(t)
	r := fixtureTorrent(300)
	got := GetTorrentByHash(r.InfoHash)
	if got == nil || got.Title != r.Title || string(got.InfoBytes) != string(r.InfoBytes) {
		t.Fatal("metadata not preserved")
	}
	if d.gets != 1 || d.lists != 0 {
		t.Fatalf("lookup scanned database: gets=%d lists=%d", d.gets, d.lists)
	}
	before := string(d.rows[fixtureTorrent(301).InfoHash.HexString()])
	r.Title = "changed"
	AddTorrent(r)
	if d.sets != 1 || d.lists != 0 {
		t.Fatal("saving rewrote unrelated records")
	}
	if string(d.rows[fixtureTorrent(301).InfoHash.HexString()]) != before {
		t.Fatal("unrelated record changed")
	}
	if GetTorrentByHash(fixtureTorrent(2000).InfoHash) != nil {
		t.Fatal("unknown hash found")
	}
}
func TestIndexedTorrentConcurrentLookups(t *testing.T) {
	d := setupIndex(t)
	var wg sync.WaitGroup
	for n := 0; n < 32; n++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				r := fixtureTorrent((n*100+i)%950 + 1)
				got := GetTorrentByHash(r.InfoHash)
				if got == nil {
					t.Error("missing row")
				}
				if i%10 == 0 {
					AddTorrent(r)
				}
			}
		}(n)
	}
	wg.Wait()
	if d.lists != 0 {
		t.Fatal("concurrent lookup scanned catalogue")
	}
}
