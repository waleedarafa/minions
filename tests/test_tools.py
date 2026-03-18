from __future__ import annotations


import pytest


@pytest.fixture
def temp_dir(tmp_path):
    return str(tmp_path)


def test_file_ops_read_write(temp_dir):
    from src.tools.builtin.file_ops import create_file, read_file, set_working_dir
    set_working_dir(temp_dir)

    result = create_file("test.txt", "hello world")
    assert "Created" in result

    content = read_file("test.txt")
    assert content == "hello world"


def test_file_ops_edit(temp_dir):
    from src.tools.builtin.file_ops import create_file, edit_file, read_file, set_working_dir
    set_working_dir(temp_dir)

    create_file("test.txt", "foo bar baz")
    result = edit_file("test.txt", "bar", "qux")
    assert "Successfully" in result

    content = read_file("test.txt")
    assert "qux" in content
    assert "bar" not in content


def test_file_ops_delete(temp_dir):
    from src.tools.builtin.file_ops import create_file, delete_file, set_working_dir
    set_working_dir(temp_dir)

    create_file("to_delete.txt", "bye")
    result = delete_file("to_delete.txt")
    assert "Deleted" in result

    result2 = delete_file("to_delete.txt")
    assert "Error" in result2


def test_file_not_found(temp_dir):
    from src.tools.builtin.file_ops import read_file, set_working_dir
    set_working_dir(temp_dir)
    result = read_file("nonexistent.txt")
    assert "Error" in result


def test_list_files(temp_dir):
    from src.tools.builtin.file_ops import create_file, list_files, set_working_dir
    set_working_dir(temp_dir)
    create_file("a.txt", "")
    create_file("b.txt", "")

    result = list_files(".")
    assert "a.txt" in result
    assert "b.txt" in result


def test_glob_search(temp_dir):
    from src.tools.builtin.file_ops import create_file, set_working_dir
    from src.tools.builtin.search import glob_search, set_working_dir as sg_wd
    set_working_dir(temp_dir)
    sg_wd(temp_dir)

    create_file("foo.py", "x=1")
    create_file("bar.py", "y=2")

    result = glob_search("*.py", path=temp_dir)
    assert "foo.py" in result or "bar.py" in result


def test_grep_search(temp_dir):
    from src.tools.builtin.file_ops import create_file, set_working_dir
    from src.tools.builtin.search import grep_search, set_working_dir as sg_wd
    set_working_dir(temp_dir)
    sg_wd(temp_dir)

    create_file("code.py", "def hello_world():\n    pass\n")
    result = grep_search("hello_world", path=temp_dir)
    assert "hello_world" in result
