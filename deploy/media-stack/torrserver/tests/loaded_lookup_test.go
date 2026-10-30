package torr

import (
	"github.com/anacrolix/torrent/metainfo"
	"testing"
)

func TestLoadedLookupDoesNotRequireDatabaseOrClient(t *testing.T) {
	old := bts
	defer func() { bts = old }()
	h := metainfo.NewHashFromHex("1234567890123456789012345678901234567890")
	live := &Torrent{}
	bts = &BTServer{torrents: map[metainfo.Hash]*Torrent{h: live}}
	if GetLoadedTorrent(h.HexString()) != live {
		t.Fatal("loaded torrent missing")
	}
	if GetLoadedTorrent("0000000000000000000000000000000000000000") != nil {
		t.Fatal("dormant torrent was restored")
	}
}
