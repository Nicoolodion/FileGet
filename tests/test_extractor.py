from __future__ import annotations

from pathlib import Path

from plex_get.extractor import group_rar_volumes, volume_group_key


def test_volume_group_key_part() -> None:
    assert volume_group_key('foo.part1.rar') == ('foo.rar', 1)
    assert volume_group_key('foo.part10.rar') == ('foo.rar', 10)
    assert volume_group_key('foo.part1.RAR') == ('foo.rar', 1)
    assert volume_group_key('foo.rar') == ('foo.rar', 0)
    assert volume_group_key('foo.txt') == ('foo.txt', 0)


def test_group_rar_volumes_multipart(tmp_path: Path) -> None:
    files = []
    for n in ['a.part1.rar', 'a.part2.rar', 'a.part3.rar']:
        p = tmp_path / n
        p.write_bytes(b'')
        files.append(p)
    groups = group_rar_volumes(files)
    assert len(groups) == 1
    assert [p.name for p in groups[0]] == ['a.part1.rar', 'a.part2.rar', 'a.part3.rar']


def test_group_rar_volumes_mixed(tmp_path: Path) -> None:
    files = []
    for n in ['a.part1.rar', 'a.part2.rar', 'b.rar', 'c.part1.rar', 'c.part2.rar']:
        p = tmp_path / n
        p.write_bytes(b'')
        files.append(p)
    groups = group_rar_volumes(files)
    by_key = {volume_group_key(g[0].name)[0]: g for g in groups}
    assert set(by_key.keys()) == {'a.rar', 'b.rar', 'c.rar'}
    assert [p.name for p in by_key['a.rar']] == ['a.part1.rar', 'a.part2.rar']
    assert [p.name for p in by_key['b.rar']] == ['b.rar']
    assert [p.name for p in by_key['c.rar']] == ['c.part1.rar', 'c.part2.rar']


def test_group_rar_volumes_handles_unordered_parts(tmp_path: Path) -> None:
    files = []
    for n in ['a.part3.rar', 'a.part1.rar', 'a.part2.rar']:
        p = tmp_path / n
        p.write_bytes(b'')
        files.append(p)
    groups = group_rar_volumes(files)
    assert len(groups) == 1
    assert [p.name for p in groups[0]] == ['a.part1.rar', 'a.part2.rar', 'a.part3.rar']
