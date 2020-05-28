from enum import Enum
from itertools import chain

from .. import Array, Dictionary, Name, Object


class PageLocation(Enum):
    XYZ = 1
    Fit = 2
    FitH = 3
    FitV = 4
    FitR = 5
    FitB = 6
    FitBH = 7
    FitBV = 8


PAGE_LOCATION_ARGS = {
    PageLocation.XYZ: ('left', 'top', 'zoom'),
    PageLocation.FitH: ('top',),
    PageLocation.FitV: ('left',),
    PageLocation.FitR: ('left', 'bottom', 'right', 'top'),
    PageLocation.FitBH: ('top',),
    PageLocation.FitBV: ('left',),
}
ALL_PAGE_LOCATION_KWARGS = set(chain.from_iterable(PAGE_LOCATION_ARGS.values()))


def make_page_destination(
    pdf, page_num: int, page_location: (PageLocation, str) = None, **kwargs
) -> Array:
    """
    Creates a destination ``Array`` with reference to a Pdf document's page number.

    Arguments:
        pdf: ``Pdf`` document object.
        page_num: Page number (zero-based).
        page_location: Optional page location, as a string or ``PageLocation`` enum.
        kwargs: Optional keyword arguments for the page location, e.g. ``top``.
    """
    res = [pdf.pages[page_num]]
    if page_location:
        if isinstance(page_location, PageLocation):
            loc_key = page_location
            loc_str = loc_key.name
        else:
            loc_str = page_location
            try:
                loc_key = PageLocation[loc_str]
            except KeyError:
                raise ValueError(
                    "Invalid or unsupported page location type {0}".format(loc_str)
                )
        res.append(Name('/{0}'.format(loc_str)))
        dest_arg_names = PAGE_LOCATION_ARGS.get(loc_key)
        if dest_arg_names:
            res.extend(kwargs.get(k, 0) for k in dest_arg_names)
    else:
        res.append(Name.Fit)
    return Array(res)


class OutlineStructureError(Exception):
    pass


class OutlineItem:
    """Manages a single item in a PDF document outlines structure, including
    nested items.

    Arguments:
        title: Title of the outlines item.
        destination: Page number, destination name, or any other PDF object
            to be used as a reference when clicking on the outlines entry. Note
            this should be ``None`` if an action is used instead. If set to a
            page number, it will be resolved to a reference at the time of
            writing the outlines back to the document.
        page_location: Supplemental page location for a page number
            in ``destination``, e.g. ``PageLocation.Fit``. May also be
            a simple string such as ``'FitH'``.
        action: Action to perform when clicking on this item. Will be ignored
           during writing if ``destination`` is also set.
        obj: ``Dictionary`` object representing this outlines item in a ``Pdf``.
            May be ``None`` for creating a new object. If present, an existing
            object is modified in-place during writing and original attributes
            are retained.
        kwargs: Additional keyword arguments. Any of ``left``, ``top``,
            ``bottom``, ``right``, or ``zoom``, they will be processed for
            usage of extended page location types, e.g. /XYZ.

    This object does not contain any information about higher-level or
    neighboring elements.
    """

    def __init__(
        self,
        title: str,
        destination: (int, str, Object) = None,
        page_location: (PageLocation, str) = None,
        action: Dictionary = None,
        obj: Dictionary = None,
        **kwargs
    ):
        self.title = title
        self.destination = destination
        self.page_location = page_location
        self.page_location_kwargs = {}
        self.action = action
        self.obj = obj
        for k, v in kwargs.items():
            if k in ALL_PAGE_LOCATION_KWARGS:
                self.page_location_kwargs[k] = v
            else:
                raise ValueError("Invalid keyword argument {0}".format(k))
        self.is_closed = False
        self.children = []

    def __str__(self):
        if self.children:
            if self.is_closed:
                oc_indicator = '[+]'
            else:
                oc_indicator = '[-]'
        else:
            oc_indicator = '[ ]'
        if self.destination is not None:
            dest = self.destination
        else:
            dest = '<Action>'
        return '{1} {0.title} -> {2}'.format(self, oc_indicator, dest)

    def __repr__(self):
        return '<{0.__class__.__name__}: "{0.title}">'.format(self)

    @classmethod
    def from_dictionary_object(cls, obj: Dictionary):
        """Creates a ``OutlineItem`` from a PDF document's ``Dictionary``
        object. Does not process nested items.

        Arguments:
            obj: ``Dictionary`` object representing a single outline node.
        """
        title = str(obj.Title)
        destination = obj.get(Name.Dest)
        action = obj.get(Name.A)
        return cls(title, destination=destination, action=action, obj=obj)

    def to_dictionary_object(self, pdf, create_new=False) -> Dictionary:
        """Creates a ``Dictionary`` object from this outline node's data,
        or updates the existing object.
        Page numbers are resolved to a page reference on the input
        ``Pdf`` object.

        Arguments:
            pdf: PDF document object.
            create_new: If set to ``True``, creates a new object instead of
                modifying an existing one in-place.
        """
        if create_new or self.obj is None:
            self.obj = obj = pdf.make_indirect(Dictionary())
        else:
            obj = self.obj
        obj.Title = self.title
        if self.destination is not None:
            if isinstance(self.destination, int):
                self.destination = make_page_destination(
                    pdf,
                    self.destination,
                    self.page_location,
                    **self.page_location_kwargs,
                )
            obj.Dest = self.destination
            if Name.A in obj:
                del obj.A
        elif self.action is not None:
            obj.A = self.action
            if Name.Dest in obj:
                del obj.Dest
        return obj


class Outline:
    """Maintains a intuitive interface for creating and editing PDF document outlines,
    according to the PDF reference manual (ISO32000:2008) section 12.3.

    Arguments:
        pdf: PDF document object.
        max_depth: Maximum recursion depth to consider when reading the outline.
        strict: If set to ``False`` (default) silently ignores structural errors.
            Setting it to ``True`` raises a ``OutlineStructureError`` if any object
            references re-occur while the outline is being read or written.

    See Also:
        :meth:`pikepdf.Pdf.open_outline`
    """

    def __init__(self, pdf, max_depth=15, strict=False):
        self._root = None
        self._pdf = pdf
        self._max_depth = max_depth
        self._strict = strict
        self._updating = False

    def __str__(self):
        return str(self.root)

    def __repr__(self):
        return '<{0.__class__.__name__}: {1} items>'.format(self, len(self.root))

    def __enter__(self):
        self._updating = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                return
            self._save()
        finally:
            self._updating = False

    def _save_level_outline(
        self, parent: Dictionary, outline_items: list, level: int, visited_objs: set
    ):
        count = 0
        prev = None
        first = None
        for item in outline_items:
            out_obj = item.to_dictionary_object(self._pdf)
            objgen = out_obj.objgen
            if objgen in visited_objs:
                if self._strict:
                    raise OutlineStructureError(
                        "Outline object {0} reoccurred in structure".format(objgen)
                    )
                out_obj = item.to_dictionary_object(self._pdf, create_new=True)
            else:
                visited_objs.add(objgen)

            out_obj.Parent = parent
            count += 1
            if prev is not None:
                prev.Next = out_obj
                out_obj.Prev = prev
            else:
                first = out_obj
                if Name.Prev in out_obj:
                    del out_obj.Prev
            prev = out_obj
            if level < self._max_depth:
                sub_items = item.children
            else:
                sub_items = ()
            self._save_level_outline(out_obj, sub_items, level + 1, visited_objs)
            if item.is_closed:
                out_obj.Count = -out_obj.Count
            else:
                count += out_obj.Count
        if count:
            if Name.Next in prev:
                del prev.Next
            parent.First = first
            parent.Last = prev
        else:
            if Name.First in parent:
                del parent.First
            if Name.Last in parent:
                del parent.Last
        parent.Count = count

    def _load_level_outline(
        self, first_obj: Dictionary, outline_items: list, level: int, visited_objs: set
    ):
        current_obj = first_obj
        while current_obj:
            objgen = current_obj.objgen
            if objgen in visited_objs:
                if self._strict:
                    raise OutlineStructureError(
                        "Outline object {0} reoccurred in structure".format(objgen)
                    )
                return
            visited_objs.add(objgen)

            item = OutlineItem.from_dictionary_object(current_obj)
            first_child = current_obj.get(Name.First)
            if first_child is not None and level < self._max_depth:
                self._load_level_outline(
                    first_child, item.children, level + 1, visited_objs
                )
                count = current_obj.get(Name.Count)
                if count and count < 0:
                    item.is_closed = True
            outline_items.append(item)
            current_obj = current_obj.get(Name.Next)

    def _save(self):
        if self._root is None:
            return
        if Name.Outlines in self._pdf.Root:
            outlines = self._pdf.Root.Outlines
        else:
            self._pdf.Root.Outlines = outlines = self._pdf.make_indirect(
                Dictionary(Type=Name.Outlines)
            )
        self._save_level_outline(outlines, self._root, 0, set())

    def _load(self):
        self._root = root = []
        if Name.Outlines not in self._pdf.Root:
            return
        outlines = self._pdf.Root.Outlines or {}
        first_obj = outlines.get(Name.First)
        if first_obj:
            self._load_level_outline(first_obj, root, 0, set())

    @property
    def root(self):
        if self._root is None:
            self._load()
        return self._root
