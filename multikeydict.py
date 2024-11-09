class MultiKeyDict:
    def __init__(self):
        self._data = {}
        self._keys_map = {}

    def __len__(self):
        return len(self._data)

    def __setitem__(self, keys, value):
        if not isinstance(keys, tuple):
            keys = (keys,)
        for key in keys:
            self._keys_map[key] = keys
        self._data[keys] = value

    def __getitem__(self, key):
        keys = self._keys_map.get(key)
        if keys is None:
            raise KeyError(key)
        return self._data[keys]

    def __delitem__(self, key):
        keys = self._keys_map.get(key)
        if keys is None:
            raise KeyError(key)
        for k in keys:
            del self._keys_map[k]
        del self._data[keys]

    def __contains__(self, key):
        return key in self._keys_map

    def __repr__(self):
        return f"{self.__class__.__name__}({self._data})"

    def items(self):
        return self._data.items()

    def get(self, key, default=None):
        keys = self._keys_map.get(key)
        if keys is None:
            return default
        return self._data[keys]

    def get_keys(self, key, default=None):
        keys = self._keys_map.get(key)
        if keys is None:
            return default
        return keys

    def pop(self, key, default=None):
        if key in self:
            value = self[key]
            del self[key]
            return value
        return default

    def setdefault(self, keys, default=None):
        if not isinstance(keys, tuple):
            keys = (keys,)
        if keys in self._data:
            return self._data[keys]
        self[keys] = default
        return default

    def serialize(self):
        return [list(keys) + [value] for keys, value in self._data.items()]

    @classmethod
    def deserialize(cls, serialized_data):
        multi_key_dict = cls()
        for item in serialized_data:
            *keys, value = item
            multi_key_dict[tuple(keys)] = value
        return multi_key_dict