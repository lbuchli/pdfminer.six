""" Adobe character mapping (CMap) support.

CMaps provide the mapping between character codes and Unicode
code-points to character ids (CIDs).

More information is available on the Adobe website:

  http://opensource.adobe.com/wiki/display/cmap/CMap+Resources

"""

import sys
import os
import os.path
import gzip
import pickle as pickle
import struct
import logging
from .psparser import PSStackParser
from .psparser import PSSyntaxError
from .psparser import PSEOF
from .psparser import PSLiteral
from .psparser import literal_name
from .psparser import KWD
from .encodingdb import name2unicode
from .utils import choplist
from .utils import nunpack


log = logging.getLogger(__name__)


class CMapError(Exception):
    pass


class CMapBase:

    debug = 0

    def __init__(self, **kwargs):
        self.attrs = kwargs.copy()
        return

    def is_vertical(self):
        return self.attrs.get('WMode', 0) != 0

    def set_attr(self, k, v):
        self.attrs[k] = v
        return

    def add_code2cid(self, code, cid):
        return

    def add_cid2unichr(self, cid, code):
        return

    def use_cmap(self, cmap):
        return


class CMap(CMapBase):

    def __init__(self, **kwargs):
        CMapBase.__init__(self, **kwargs)
        self.code2cid = {}
        return

    def __repr__(self):
        return '<CMap: %s>' % self.attrs.get('CMapName')

    def use_cmap(self, cmap):
        assert isinstance(cmap, CMap), str(type(cmap))

        def copy(dst, src):
            for (k, v) in src.items():
                if isinstance(v, dict):
                    d = {}
                    dst[k] = d
                    copy(d, v)
                else:
                    dst[k] = v
        copy(self.code2cid, cmap.code2cid)
        return

    def decode(self, code):
        log.debug('decode: %r, %r', self, code)
        d = self.code2cid
        for i in iter(code):
            if i in d:
                d = d[i]
                if isinstance(d, int):
                    yield d
                    d = self.code2cid
            else:
                d = self.code2cid
        return

    def dump(self, out=sys.stdout, code2cid=None, code=None):
        if code2cid is None:
            code2cid = self.code2cid
            code = ()
        for (k, v) in sorted(code2cid.items()):
            c = code+(k,)
            if isinstance(v, int):
                out.write('code %r = cid %d\n' % (c, v))
            else:
                self.dump(out=out, code2cid=v, code=c)
        return


class IdentityCMap(CMapBase):

    def decode(self, code):
        n = -(-len(code)//2) # ceil division
        padding = b'' if len(code) % 2 == 0 else b'\x00'
        if padding:
            log.warn("Added byte padding where it should not have been nessessary. Broken pdf.")
        return struct.unpack('>%dH' % n, padding + code)


class IdentityCMapByte(IdentityCMap):

    def decode(self, code):
        n = len(code)
        if n:
            return struct.unpack('>%dB' % n, code)
        else:
            return ()


class UnicodeMap(CMapBase):

    def __init__(self, **kwargs):
        CMapBase.__init__(self, **kwargs)
        self.cid2unichr = {}
        return

    def __repr__(self):
        return '<UnicodeMap: %s>' % self.attrs.get('CMapName')

    def get_unichr(self, cid):
        log.debug('get_unichr: %r, %r', self, cid)
        return self.cid2unichr[cid]

    def dump(self, out=sys.stdout):
        for (k, v) in sorted(self.cid2unichr.items()):
            out.write('cid %d = unicode %r\n' % (k, v))
        return


class FileCMap(CMap):

    def add_code2cid(self, code, cid):
        assert isinstance(code, str) and isinstance(cid, int),\
            str((type(code), type(cid)))
        d = self.code2cid
        for c in code[:-1]:
            c = ord(c)
            if c in d:
                d = d[c]
            else:
                t = {}
                d[c] = t
                d = t
        c = ord(code[-1])
        d[c] = cid
        return


class FileUnicodeMap(UnicodeMap):

    def add_cid2unichr(self, cid, code):
        assert isinstance(cid, int), str(type(cid))
        if isinstance(code, PSLiteral):
            # Interpret as an Adobe glyph name.
            self.cid2unichr[cid] = name2unicode(code.name)
        elif isinstance(code, bytes):
            # Interpret as UTF-16BE.
            self.cid2unichr[cid] = code.decode('UTF-16BE', 'ignore')
        elif isinstance(code, int):
            self.cid2unichr[cid] = chr(code)
        else:
            raise TypeError(code)
        return


class PyCMap(CMap):

    def __init__(self, name, module):
        CMap.__init__(self, CMapName=name)
        self.code2cid = module.CODE2CID
        if module.IS_VERTICAL:
            self.attrs['WMode'] = 1
        return


class PyUnicodeMap(UnicodeMap):

    def __init__(self, name, module, vertical):
        UnicodeMap.__init__(self, CMapName=name)
        if vertical:
            self.cid2unichr = module.CID2UNICHR_V
            self.attrs['WMode'] = 1
        else:
            self.cid2unichr = module.CID2UNICHR_H
        return


class CMapDB:

    _cmap_cache = {}
    _umap_cache = {}

    class CMapNotFound(CMapError):
        pass

    @classmethod
    def _load_data(cls, name):
        name = name.replace("\0", "")
        filename = '%s.pickle.gz' % name
        log.info('loading: %r', name)
        cmap_paths = (os.environ.get('CMAP_PATH', '/usr/share/pdfminer/'),
                      os.path.join(os.path.dirname(__file__), 'cmap'),)
        for directory in cmap_paths:
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                gzfile = gzip.open(path)
                try:
                    return type(str(name), (), pickle.loads(gzfile.read()))
                finally:
                    gzfile.close()
        else:
            raise CMapDB.CMapNotFound(name)

    @classmethod
    def get_cmap(cls, name):
        if name == 'Identity-H':
            return IdentityCMap(WMode=0)
        elif name == 'Identity-V':
            return IdentityCMap(WMode=1)
        elif name == 'OneByteIdentityH':
            return IdentityCMapByte(WMode=0)
        elif name == 'OneByteIdentityV':
            return IdentityCMapByte(WMode=1)
        try:
            return cls._cmap_cache[name]
        except KeyError:
            pass
        data = cls._load_data(name)
        cls._cmap_cache[name] = cmap = PyCMap(name, data)
        return cmap

    @classmethod
    def get_unicode_map(cls, name, vertical=False):
        try:
            return cls._umap_cache[name][vertical]
        except KeyError:
            pass
        data = cls._load_data('to-unicode-%s' % name)
        cls._umap_cache[name] = [PyUnicodeMap(name, data, v)
                                 for v in (False, True)]
        return cls._umap_cache[name][vertical]


class CMapParser(PSStackParser):

    def __init__(self, cmap, fp):
        PSStackParser.__init__(self, fp)
        self.cmap = cmap
        # some ToUnicode maps don't have "begincmap" keyword.
        self._in_cmap = True
        return

    def run(self):
        try:
            self.nextobject()
        except PSEOF:
            pass
        return

    KEYWORD_BEGINCMAP = KWD(b'begincmap')
    KEYWORD_ENDCMAP = KWD(b'endcmap')
    KEYWORD_USECMAP = KWD(b'usecmap')
    KEYWORD_DEF = KWD(b'def')
    KEYWORD_BEGINCODESPACERANGE = KWD(b'begincodespacerange')
    KEYWORD_ENDCODESPACERANGE = KWD(b'endcodespacerange')
    KEYWORD_BEGINCIDRANGE = KWD(b'begincidrange')
    KEYWORD_ENDCIDRANGE = KWD(b'endcidrange')
    KEYWORD_BEGINCIDCHAR = KWD(b'begincidchar')
    KEYWORD_ENDCIDCHAR = KWD(b'endcidchar')
    KEYWORD_BEGINBFRANGE = KWD(b'beginbfrange')
    KEYWORD_ENDBFRANGE = KWD(b'endbfrange')
    KEYWORD_BEGINBFCHAR = KWD(b'beginbfchar')
    KEYWORD_ENDBFCHAR = KWD(b'endbfchar')
    KEYWORD_BEGINNOTDEFRANGE = KWD(b'beginnotdefrange')
    KEYWORD_ENDNOTDEFRANGE = KWD(b'endnotdefrange')

    def do_keyword(self, pos, token):
        if token is self.KEYWORD_BEGINCMAP:
            self._in_cmap = True
            self.popall()
            return
        elif token is self.KEYWORD_ENDCMAP:
            self._in_cmap = False
            return
        if not self._in_cmap:
            return
        #
        if token is self.KEYWORD_DEF:
            try:
                ((_, k), (_, v)) = self.pop(2)
                self.cmap.set_attr(literal_name(k), v)
            except PSSyntaxError:
                pass
            return

        if token is self.KEYWORD_USECMAP:
            try:
                ((_, cmapname),) = self.pop(1)
                self.cmap.use_cmap(CMapDB.get_cmap(literal_name(cmapname)))
            except PSSyntaxError:
                pass
            except CMapDB.CMapNotFound:
                pass
            return

        if token is self.KEYWORD_BEGINCODESPACERANGE:
            self.popall()
            return
        if token is self.KEYWORD_ENDCODESPACERANGE:
            self.popall()
            return

        if token is self.KEYWORD_BEGINCIDRANGE:
            self.popall()
            return
        if token is self.KEYWORD_ENDCIDRANGE:
            objs = [obj for (__, obj) in self.popall()]
            for (s, e, cid) in choplist(3, objs):
                if (not isinstance(s, bytes) or not isinstance(e, bytes) or
                   not isinstance(cid, int) or len(s) != len(e)):
                    continue
                sprefix = s[:-4]
                eprefix = e[:-4]
                if sprefix != eprefix:
                    continue
                svar = s[-4:]
                evar = e[-4:]
                s1 = nunpack(svar)
                e1 = nunpack(evar)
                vlen = len(svar)
                for i in range(e1-s1+1):
                    x = sprefix+struct.pack('>L', s1+i)[-vlen:]
                    self.cmap.add_cid2unichr(cid+i, x)
            return

        if token is self.KEYWORD_BEGINCIDCHAR:
            self.popall()
            return
        if token is self.KEYWORD_ENDCIDCHAR:
            objs = [obj for (__, obj) in self.popall()]
            for (cid, code) in choplist(2, objs):
                if isinstance(code, bytes) and isinstance(cid, int):
                    self.cmap.add_cid2unichr(cid, code)
            return

        if token is self.KEYWORD_BEGINBFRANGE:
            self.popall()
            return
        if token is self.KEYWORD_ENDBFRANGE:
            objs = [obj for (__, obj) in self.popall()]
            for (s, e, code) in choplist(3, objs):
                if (not isinstance(s, bytes) or not isinstance(e, bytes) or
                   len(s) != len(e)):
                    continue
                s1 = nunpack(s)
                e1 = nunpack(e)
                if isinstance(code, list):
                    for i in range(e1-s1+1):
                        self.cmap.add_cid2unichr(s1+i, code[i])
                else:
                    var = code[-4:]
                    base = nunpack(var)
                    prefix = code[:-4]
                    vlen = len(var)
                    for i in range(e1-s1+1):
                        x = prefix+struct.pack('>L', base+i)[-vlen:]
                        self.cmap.add_cid2unichr(s1+i, x)
            return

        if token is self.KEYWORD_BEGINBFCHAR:
            self.popall()
            return
        if token is self.KEYWORD_ENDBFCHAR:
            objs = [obj for (__, obj) in self.popall()]
            for (cid, code) in choplist(2, objs):
                if isinstance(cid, bytes) and isinstance(code, bytes):
                    self.cmap.add_cid2unichr(nunpack(cid), code)
            return

        if token is self.KEYWORD_BEGINNOTDEFRANGE:
            self.popall()
            return
        if token is self.KEYWORD_ENDNOTDEFRANGE:
            self.popall()
            return

        self.push((pos, token))
        return


def main(argv):
    args = argv[1:]
    for fname in args:
        fp = open(fname, 'rb')
        cmap = FileUnicodeMap()
        CMapParser(cmap, fp).run()
        fp.close()
        cmap.dump()
    return


if __name__ == '__main__':
    sys.exit(main(sys.argv))
