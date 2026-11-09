"""Cleanup failures must remain false and explain the exact failing operation."""
import errno

from test_runner_destination import runner


def test_wipe_reports_listdir_error(monkeypatch, capsys):
    def denied(path):
        raise PermissionError(errno.EACCES, 'Permission denied', path)
    monkeypatch.setattr(runner.os, 'listdir', denied)
    assert runner.wipe_dir_contents('/fake/cache') is False
    err = capsys.readouterr().err
    assert 'cleanup listdir' in err and 'errno=13' in err and '/fake/cache' in err


def test_remove_reports_rmtree_error(monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(runner.os, 'lstat', lambda p: SimpleNamespace(st_mode=0o40700))
    monkeypatch.setattr(runner, 'make_removable', lambda p: None)
    monkeypatch.setattr(runner.os.path, 'lexists', lambda p: True)
    def denied(path):
        raise PermissionError(errno.EPERM, 'Operation not permitted', path)
    monkeypatch.setattr(runner.shutil, 'rmtree', denied)
    assert runner.remove_tree('/fake/T/com.apple.trustd') is False
    err = capsys.readouterr().err
    assert 'cleanup remove' in err and 'errno=1' in err and '/fake/T/com.apple.trustd' in err


def test_missing_cleanup_path_is_success(monkeypatch, capsys):
    def missing(path):
        raise FileNotFoundError(errno.ENOENT, 'No such file', path)
    monkeypatch.setattr(runner.os, 'listdir', missing)
    assert runner.wipe_dir_contents('/fake/missing') is True
    assert capsys.readouterr().err == ''
