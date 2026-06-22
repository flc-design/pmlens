# pm-server — compatibility shim for PM Lens

**PM Server has been renamed to PM Lens.** The project now ships on PyPI as
[`pmlens`](https://pypi.org/project/pmlens/).

This `pm-server` distribution is a thin compatibility wrapper: it contains no code
of its own and simply depends on `pmlens`, so existing workflows keep working:

```sh
pip install pm-server     # pulls in pmlens, which provides the `pm-server` command
uvx pm-server             # same
```

New users should install **`pmlens`** directly:

```sh
pip install pmlens
```

The last standalone `pm-server` build remains available as `pm-server==0.10.0`.

— FLC design Co., Ltd. · MIT License
