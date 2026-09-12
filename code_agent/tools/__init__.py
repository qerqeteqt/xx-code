"""工具集。

- filesystem.py  仓库内文件读写（read_file / write_file / edit_file / list_dir）
- search.py      代码搜索（glob_search / grep_search，自研以规避内置中间件在
                 Windows 上跳过含中文文件的缺陷）
- command.py     shell 执行 + 危险命令 HITL（M3）
- web.py         联网检索（M4）
"""
