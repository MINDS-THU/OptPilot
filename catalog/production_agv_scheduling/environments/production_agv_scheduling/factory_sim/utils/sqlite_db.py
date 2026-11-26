import json
import sqlite3
import threading
from typing import Dict

from factory_sim.utils.config_loader import load_factory_config
from factory_sim.config.schemas import DATABASE_SCHEMA


class SimulationDatabaseError(RuntimeError):
    """A trace write or close failed and the retained evidence is unusable."""


class SimulationDatabase:
    def __init__(self, db_path: str, layout_config: Dict):
        self.layout_config = layout_config
        self._lock = threading.RLock()
        self.conn = None
        self._closed = False
        self._failure: SimulationDatabaseError | None = None
        try:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_table()
        except Exception:
            connection = self.conn
            self.conn = None
            self._closed = True
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise
    
    def _init_table(self):
        self._get_table_names()
        self.schema_map = DATABASE_SCHEMA
        scripts = []

        def add_scripts(names, columns):
            if not names:
                return
            column_sql = ', '.join(columns)
            for name in names:
                scripts.append(f'DROP TABLE IF EXISTS "{name}";')
                scripts.append(f'CREATE TABLE "{name}" ({column_sql});')

        add_scripts(self.warehouse_table, self.schema_map['warehouses'])
        add_scripts(self.order_table, self.schema_map['orders'])
        add_scripts(self.station_table, self.schema_map['stations'])
        add_scripts(self.qualitycheck_table, self.schema_map['qualitychecks'])
        add_scripts(self.agv_table, self.schema_map['agvs'])
        add_scripts(self.ordinary_conveyor_table, self.schema_map['conveyors'])
        add_scripts(self.triple_buffer_conveyor_table, self.schema_map['triple_conveyors'])
        add_scripts(self.fault_table, self.schema_map['faults'])
        add_scripts(self.kpi_table, self.schema_map['kpi'])
        add_scripts(self.response_table, self.schema_map['response'])

        if scripts:
            cursor = self.conn.cursor()
            cursor.executescript('\n'.join(scripts))
            self.conn.commit()

    def _get_table_names(self):
        self.warehouse_table = [warehouse['id'].lower() for warehouse in self.layout_config.get('warehouses', [])]
        self.order_table = ['order']
        self.station_table = [
            '_'.join([line['name'].lower(), station['id'].lower()])
            for line in self.layout_config.get('production_lines', [])
            for station in line.get('stations', [])
            if station.get('id', '').lower().startswith('station')
        ]
        self.qualitycheck_table = [
            '_'.join([line['name'].lower(), station['id'].lower()])
            for line in self.layout_config.get('production_lines', [])
            for station in line.get('stations', [])
            if station.get('id', '').lower().startswith('quality')
        ]
        self.agv_table = [
            '_'.join([line['name'].lower(), agv['id'].lower()])
            for line in self.layout_config.get('production_lines', [])
            for agv in line.get('agvs', [])
        ]
        self.ordinary_conveyor_table = [
            '_'.join([line['name'].lower(), conveyor['id'].lower()])
            for line in self.layout_config.get('production_lines', [])
            for conveyor in line.get('conveyors', [])
            if conveyor.get('capacity') is not None
        ]
        self.triple_buffer_conveyor_table = [
            '_'.join([line['name'].lower(), conveyor['id'].lower()])
            for line in self.layout_config.get('production_lines', [])
            for conveyor in line.get('conveyors', [])
            if conveyor.get('capacity') is None
        ]
        self.fault_table = ['_'.join([line['name'].lower(), 'fault']) for line in self.layout_config.get('production_lines', [])]
        self.response_table = ['_'.join([line['name'].lower(), 'response']) for line in self.layout_config.get('production_lines', [])]
        self.kpi_table = ['kpi']
    
    def insert_data(self, table_name: str, data: Dict):
        normalized = {key: self._normalize_value(value) for key, value in data.items()}
        columns = ', '.join(normalized.keys())
        placeholders = ', '.join(['?'] * len(normalized))
        sql = f'INSERT INTO "{table_name}" ({columns}) VALUES ({placeholders})'
        with self._lock:
            if self._failure is not None:
                raise self._failure
            if self._closed or self.conn is None:
                raise self._record_failure(
                    "write",
                    sqlite3.ProgrammingError("database connection is closed"),
                )
            try:
                cursor = self.conn.cursor()
                self._execute_insert(cursor, sql, tuple(normalized.values()))
            except sqlite3.Error as exc:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                raise self._record_failure(f"insert into {table_name}", exc) from exc

    def _execute_insert(self, cursor, sql: str, values: tuple) -> None:
        """Execute one atomic insert; kept separate for failure-injection tests."""

        cursor.execute(sql, values)
        assert self.conn is not None
        self.conn.commit()

    def _record_failure(
        self, operation: str, exc: BaseException
    ) -> SimulationDatabaseError:
        if self._failure is None:
            self._failure = SimulationDatabaseError(
                f"SQLite trace {operation} failed: {type(exc).__name__}: {exc}"
            )
        return self._failure

    def _normalize_value(self, value):
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, set):
            return json.dumps(sorted(value), ensure_ascii=False)
        if value is None or isinstance(value, (int, float, str, bytes)):
            return value
        return str(value)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                if self._failure is not None:
                    raise self._failure
                return
            connection = self.conn
            self.conn = None
            self._closed = True
            if connection is not None:
                try:
                    self._close_connection(connection)
                except Exception as exc:
                    self._record_failure("close", exc)
            if self._failure is not None:
                raise self._failure

    @staticmethod
    def _close_connection(connection) -> None:
        """Close the SQLite handle; kept separate for failure-injection tests."""

        connection.close()


if __name__ == "__main__":
    layout_config = load_factory_config('factory_layout_multi.yml')
    db = SimulationDatabase('simulation.db', layout_config)
    print("KPI tables:", db.kpi_table)
