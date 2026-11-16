-- SQLite 3.49+: clamp parsed fractions to .999; preserve normal millisecond rounding.

CREATE OR REPLACE FUNCTION tam_compat._parse_hms(value text, p tam_compat.sqlite_datetime)
RETURNS tam_compat.sqlite_datetime
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    parts text[];
    rest text;
    hh integer;
    mm integer;
    ss double precision := 0;
    tz_hours integer;
    tz_minutes integer;
BEGIN
    parts := regexp_match(value, '^([0-9]{2}):([0-9]{2})(.*)$');
    IF parts IS NULL THEN
        p.err := true;
        RETURN p;
    END IF;
    hh := parts[1]::integer;
    mm := parts[2]::integer;
    rest := parts[3];
    IF hh > 24 OR mm > 59 THEN
        p.err := true;
        RETURN p;
    END IF;
    IF left(rest, 1) = ':' THEN
        parts := regexp_match(rest, '^:([0-9]{2})(.*)$');
        IF parts IS NULL OR parts[1]::integer > 59 THEN
            p.err := true;
            RETURN p;
        END IF;
        ss := parts[1]::integer;
        rest := parts[2];
        parts := regexp_match(rest, '^\.([0-9]+)(.*)$');
        IF parts IS NOT NULL THEN
            ss := ss + least(0.999::double precision, parts[1]::numeric::double precision / power(10::double precision, length(parts[1])));
            rest := parts[2];
        END IF;
    END IF;
    -- parseTimezone
    rest := ltrim(rest, E' \t\n\r\f\v');
    p.tz := 0;
    IF rest = '' THEN
        NULL;
    ELSIF left(rest, 1) IN ('Z', 'z') THEN
        IF ltrim(substr(rest, 2), E' \t\n\r\f\v') <> '' THEN
            p.err := true;
            RETURN p;
        END IF;
    ELSE
        parts := regexp_match(rest, '^([+-])([0-9]{2}):([0-9]{2})\s*$');
        IF parts IS NULL THEN
            p.err := true;
            RETURN p;
        END IF;
        tz_hours := parts[2]::integer;
        tz_minutes := parts[3]::integer;
        IF tz_hours > 14 OR tz_minutes > 59 THEN
            p.err := true;
            RETURN p;
        END IF;
        p.tz := (CASE WHEN parts[1] = '-' THEN -1 ELSE 1 END) * (tz_minutes + tz_hours * 60);
    END IF;
    p.valid_jd := false;
    p.raw_s := false;
    p.valid_hms := true;
    p.h := hh;
    p.mi := mm;
    p.s := ss;
    p.valid_tz := p.tz <> 0;
    RETURN p;
END
$$;

CREATE OR REPLACE FUNCTION tam_compat._iso_jd(value text) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    core text := value;
    n integer;
    y integer;
    m integer;
    d integer;
    hh integer := 0;
    mm integer := 0;
    ss double precision := 0;
    fraction text;
BEGIN
    -- YYYY-MM-DD[(T| )HH:MM[:SS[.F+]]][Z], checked positionally (PostgreSQL regular
    -- expressions with optional capture groups cost ~50 µs per call).
    IF right(core, 1) = 'Z' THEN
        core := left(core, -1);
    END IF;
    n := length(core);
    IF n < 10 OR substr(core, 5, 1) <> '-' OR substr(core, 8, 1) <> '-'
       OR translate(substr(core, 1, 4) || substr(core, 6, 2) || substr(core, 9, 2), '0123456789', '') <> '' THEN
        RETURN NULL;
    END IF;
    IF n = 10 THEN
        IF core <> value THEN
            RETURN NULL;
        END IF;
    ELSE
        IF n < 16 OR substr(core, 11, 1) NOT IN (' ', 'T') OR substr(core, 14, 1) <> ':'
           OR translate(substr(core, 12, 2) || substr(core, 15, 2), '0123456789', '') <> '' THEN
            RETURN NULL;
        END IF;
        hh := substr(core, 12, 2)::integer;
        mm := substr(core, 15, 2)::integer;
        IF n > 16 THEN
            IF n < 19 OR substr(core, 17, 1) <> ':' OR translate(substr(core, 18, 2), '0123456789', '') <> '' THEN
                RETURN NULL;
            END IF;
            ss := substr(core, 18, 2)::integer;
            IF n > 19 THEN
                fraction := substr(core, 21);
                IF substr(core, 20, 1) <> '.' OR fraction = '' OR translate(fraction, '0123456789', '') <> '' THEN
                    RETURN NULL;
                END IF;
            END IF;
        END IF;
    END IF;
    y := substr(core, 1, 4)::integer;
    m := substr(core, 6, 2)::integer;
    d := substr(core, 9, 2)::integer;
    IF m NOT BETWEEN 1 AND 12 OR d NOT BETWEEN 1 AND 31 OR hh > 24 OR mm > 59 OR ss > 59 THEN
        RETURN NULL;
    END IF;
    IF fraction IS NOT NULL THEN
        ss := ss + least(0.999::double precision, fraction::numeric::double precision / power(10::double precision, length(fraction)));
    END IF;
    IF m <= 2 THEN
        y := y - 1;
        m := m + 12;
    END IF;
    RETURN ((36525 * (y + 4716) / 100)::bigint + 306001 * (m + 1) / 10000 + d
            + 38 - (y + 4800) / 100 + (y + 4800) / 100 / 4) * 86400000 - 131716800000
        + hh::bigint * 3600000 + mm::bigint * 60000 + trunc(ss * 1000 + 0.5)::bigint;
END
$$;
