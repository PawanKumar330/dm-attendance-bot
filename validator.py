"""
validator.py — Google Sheet Attendance Template Validator.

Validates whether a Google Sheets worksheet strictly conforms to the expected
college attendance template.
"""

from datetime import datetime
import re
from typing import Any, Dict, List, Optional
import gspread

EXPECTED_BASE_HEADERS = [
    'S.NO',
    'Roll NO',
    'Reg.NO',
    'Student Name',
    'Present',
    'Absent',
    'Percentage'
]


def col_index_to_letter(col_idx: int) -> str:
    """Convert 1-based column index to Excel column letter (e.g. 1 -> A, 8 -> H)."""
    result = ""
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result


def normalize_header(text: str) -> str:
    """Normalize header string for resilient matching (remove spaces, dots, lowercase)."""
    return re.sub(r'[\s._-]+', '', str(text)).lower()


NORMALIZED_BASE_HEADERS = [normalize_header(h) for h in EXPECTED_BASE_HEADERS]


def is_valid_date_or_count(header_text: str) -> bool:
    """
    Check if a column header conforms to DD/MM/YYYY date format or class count (e.g. 50, 1, 2, Session 1).
    """
    clean = header_text.strip()
    if not clean:
        return False

    # Check DD/MM/YYYY, D/M/YYYY, DD-MM-YYYY, or YYYY-MM-DD
    date_patterns = [
        r'^\d{1,2}/\d{1,2}/\d{4}$',
        r'^\d{1,2}-\d{1,2}-\d{4}$',
        r'^\d{4}-\d{1,2}-\d{1,2}$',
        r'^\d{1,2}/\d{1,2}/\d{2}$',
    ]
    for pattern in date_patterns:
        if re.match(pattern, clean):
            return True

    # Check date parsing with datetime
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            datetime.strptime(clean, fmt)
            return True
        except ValueError:
            pass

    # Check if it represents an integer class count (e.g. 50, 52, 1, 2) or "Session X" / "Class X"
    if clean.isdigit():
        return True
    if re.match(r'^(session|class|c|s)?\s*\d+$', clean, re.IGNORECASE):
        return True

    return False


def validate_attendance_sheet(worksheet: Optional[gspread.Worksheet] = None,
                              raw_data: Optional[List[List[Any]]] = None) -> Dict[str, Any]:
    """
    Validate whether a worksheet strictly matches the required attendance template.

    Parameters
    ----------
    worksheet : gspread.Worksheet, optional
        The worksheet object to fetch values from.
    raw_data : list of lists, optional
        Pre-fetched or simulated 2D row data (useful for unit testing).

    Returns
    -------
    dict
        {
            'valid': bool,
            'errors': list of str (fatal errors),
            'warnings': list of str (formatting warnings),
            'student_count': int,
            'session_count': int
        }
    """
    errors: List[str] = []
    warnings: List[str] = []

    if raw_data is not None:
        rows = raw_data
    elif worksheet is not None:
        try:
            rows = worksheet.get_all_values()
        except Exception as e:
            return {
                'valid': False,
                'errors': [f"Failed to read worksheet data: {str(e)}"],
                'warnings': [],
                'student_count': 0,
                'session_count': 0
            }
    else:
        return {
            'valid': False,
            'errors': ["No worksheet or data provided for validation."],
            'warnings': [],
            'student_count': 0,
            'session_count': 0
        }

    if not rows:
        return {
            'valid': False,
            'errors': ["The worksheet is completely empty."],
            'warnings': [],
            'student_count': 0,
            'session_count': 0
        }

    # ─────────────────────────────────────────────────────────────────
    # 1. Locate Header Row (search within rows 1 to 15)
    # College sheets frequently place the header on Row 6 (after title & faculty name)
    # ─────────────────────────────────────────────────────────────────
    header_row_idx: Optional[int] = None
    header_row: List[str] = []

    search_limit = min(len(rows), 15)
    for r_idx in range(search_limit):
        row_cells = [str(c).strip() for c in rows[r_idx]]
        # Check if this row contains the base headers
        if len(row_cells) >= 7:
            cand_normalized = [normalize_header(c) for c in row_cells[:7]]
            if cand_normalized == NORMALIZED_BASE_HEADERS:
                header_row_idx = r_idx
                header_row = row_cells
                break

    if header_row_idx is None:
        # Check if there is a partial or disordered header row in rows 1-15 to give informative error
        best_match_row = None
        best_match_count = 0
        for r_idx in range(search_limit):
            row_cells = [str(c).strip() for c in rows[r_idx]]
            row_normalized = set(normalize_header(c) for c in row_cells)
            matched = sum(1 for exp in NORMALIZED_BASE_HEADERS if exp in row_normalized)
            if matched > best_match_count:
                best_match_count = matched
                best_match_row = r_idx

        if best_match_row is not None and best_match_count >= 3:
            found_headers = [str(c).strip() for c in rows[best_match_row][:10] if str(c).strip()]
            errors.append(
                f"Row {best_match_row + 1} appears to be a header row but does not strictly match "
                f"the expected sequence: {EXPECTED_BASE_HEADERS}. Found: {found_headers}"
            )
        else:
            errors.append(
                f"Header row not found within rows 1–15. Expected exact sequence in columns A–G: "
                f"{EXPECTED_BASE_HEADERS}"
            )

        return {
            'valid': False,
            'errors': errors,
            'warnings': warnings,
            'student_count': 0,
            'session_count': 0
        }

    # ─────────────────────────────────────────────────────────────────
    # 2. Check Session / Date Columns (Column H / index 7 onwards)
    # Date headers may be on the header row (e.g. Row 6) OR on upper rows (e.g. Row 1-5)
    # with class counts on Row 6.
    # ─────────────────────────────────────────────────────────────────
    session_cols: List[int] = []

    # Determine maximum number of columns across header and pre-header rows
    max_header_cols = max(len(rows[r]) for r in range(header_row_idx + 1))

    for c_idx in range(7, max_header_cols):
        # Collect values in this column across rows 0 through header_row_idx
        col_cells = [
            str(rows[r][c_idx]).strip()
            for r in range(header_row_idx + 1)
            if c_idx < len(rows[r]) and str(rows[r][c_idx]).strip()
        ]

        if col_cells:
            session_cols.append(c_idx)
            col_letter = col_index_to_letter(c_idx + 1)

            # Check if any cell in this column header area contains a valid date or class count
            has_valid_indicator = any(is_valid_date_or_count(text) for text in col_cells)
            if not has_valid_indicator:
                display_text = col_cells[-1] if col_cells else "N/A"
                warnings.append(
                    f"Column {col_letter} header '{display_text}' is not formatted as DD/MM/YYYY or class count."
                )

    session_count = len(session_cols)
    if session_count == 0:
        warnings.append("No attendance date/session columns found starting from Column H onwards.")

    # ─────────────────────────────────────────────────────────────────
    # 3. Validate Student Data Rows
    # ─────────────────────────────────────────────────────────────────
    student_count = 0
    data_start_idx = header_row_idx + 1

    for r_idx in range(data_start_idx, len(rows)):
        row = rows[r_idx]

        # Skip completely empty rows
        if not any(str(c).strip() for c in row):
            continue

        # Skip spacer / hidden rows that have no student identifier (S.NO, Roll NO, Reg.NO, Student Name)
        identity_cells = [str(row[i]).strip() for i in range(min(4, len(row))) if str(row[i]).strip()]
        if not identity_cells:
            continue

        row_num = r_idx + 1  # 1-based row number for human messages
        student_count += 1
        expected_sno = student_count

        # Pad row with empty strings if shorter than base columns + sessions
        max_needed_cols = max(7, max(session_cols) + 1 if session_cols else 7)
        cells = [str(row[i]).strip() if i < len(row) else "" for i in range(max_needed_cols)]

        sno_val = cells[0]
        roll_no = cells[1]
        reg_no = cells[2]
        student_name = cells[3]
        present_val = cells[4]
        absent_val = cells[5]
        pct_val = cells[6]

        student_identifier = f"Roll: '{roll_no or 'N/A'}' | Name: '{student_name or 'N/A'}'"

        # Check S.NO: Must be an incremental integer
        if not sno_val:
            errors.append(f"Row {row_num} ({student_identifier}): Missing 'S.NO' (expected {expected_sno}).")
        else:
            try:
                sno_int = int(sno_val)
                if sno_int != expected_sno:
                    errors.append(
                        f"Row {row_num} ({student_identifier}): Non-incremental 'S.NO' (expected {expected_sno}, got {sno_int})."
                    )
            except ValueError:
                errors.append(f"Row {row_num} ({student_identifier}): 'S.NO' must be an integer, got '{sno_val}'.")

        # Check Roll NO: Mandatory identifier string/number
        if not roll_no:
            errors.append(f"Row {row_num}: Missing mandatory 'Roll NO'.")

        # Check Reg.NO: 11-digit university registration number string
        reg_no_digits = re.sub(r'\s+', '', reg_no)
        if not re.match(r'^\d{11}$', reg_no_digits):
            errors.append(
                f"Row {row_num} ({student_identifier}): 'Reg.NO' must be an 11-digit number string, got '{reg_no}'."
            )

        # Check Student Name: Non-empty string
        if not student_name:
            errors.append(f"Row {row_num} (Roll: '{roll_no}'): 'Student Name' cannot be empty.")

        # Check Present & Absent: Integer counts
        if present_val:
            try:
                p_int = int(present_val)
                if p_int < 0:
                    errors.append(f"Row {row_num} ({student_identifier}): 'Present' count cannot be negative ({p_int}).")
            except ValueError:
                errors.append(f"Row {row_num} ({student_identifier}): 'Present' must be an integer, got '{present_val}'.")
        else:
            errors.append(f"Row {row_num} ({student_identifier}): Missing 'Present' count.")

        if absent_val:
            try:
                a_int = int(absent_val)
                if a_int < 0:
                    errors.append(f"Row {row_num} ({student_identifier}): 'Absent' count cannot be negative ({a_int}).")
            except ValueError:
                errors.append(f"Row {row_num} ({student_identifier}): 'Absent' must be an integer, got '{absent_val}'.")
        else:
            errors.append(f"Row {row_num} ({student_identifier}): Missing 'Absent' count.")

        # Check Percentage: Numeric value between 0 and 100
        clean_pct = pct_val.replace('%', '').strip()
        if clean_pct:
            try:
                pct_float = float(clean_pct)
                if not (0.0 <= pct_float <= 100.0):
                    errors.append(
                        f"Row {row_num} ({student_identifier}): 'Percentage' must be between 0 and 100, got '{pct_val}'."
                    )
            except ValueError:
                errors.append(f"Row {row_num} ({student_identifier}): 'Percentage' must be numeric, got '{pct_val}'.")
        else:
            errors.append(f"Row {row_num} ({student_identifier}): Missing 'Percentage' value.")

        # Check Attendance cells under date columns: Allowed values are 'P', 'A', or blank/empty
        for c_idx in session_cols:
            col_letter = col_index_to_letter(c_idx + 1)
            mark = cells[c_idx] if c_idx < len(cells) else ""
            mark_upper = mark.strip().upper()
            if mark_upper not in ('P', 'A', ''):
                errors.append(
                    f"Row {row_num} ({student_identifier}), Col {col_letter}: Invalid attendance mark '{mark}'. "
                    f"Allowed values are 'P', 'A', or blank."
                )

    if student_count == 0:
        errors.append("No student records found below the header row.")

    valid = len(errors) == 0

    return {
        'valid': valid,
        'errors': errors,
        'warnings': warnings,
        'student_count': student_count,
        'session_count': session_count
    }
