import json
import sqlite3
import threading
from typing import Dict

from factory_sim.utils.config_loader import load_factory_config
from factory_sim.config.schemas import DATABASE_SCHEMA


class SimulationDatabase:
    def __init__(self, db_path: str, layout_config: Dict):
        self.layout_config = layout_config
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._closed = False
        self._init_table()
    
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
        if self._closed or self.conn is None:
            return
        normalized = {key: self._normalize_value(value) for key, value in data.items()}
        columns = ', '.join(normalized.keys())
        placeholders = ', '.join(['?'] * len(normalized))
        sql = f'INSERT INTO "{table_name}" ({columns}) VALUES ({placeholders})'
        with self._lock:
            try:
                cursor = self.conn.cursor()
            except sqlite3.ProgrammingError:
                # Connection already closed elsewhere; mark as closed and ignore the write.
                self.conn = None
                self._closed = True
                return
            try:
                cursor.execute(sql, tuple(normalized.values()))
                self.conn.commit()
            except sqlite3.Error as exc:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                print(f"SQLite insert error for {table_name}: {exc}")

    def _normalize_value(self, value):
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, set):
            return json.dumps(sorted(value), ensure_ascii=False)
        if value is None or isinstance(value, (int, float, str, bytes)):
            return value
        return str(value)

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None
            self._closed = True


if __name__ == "__main__":
    layout_config = load_factory_config('factory_layout_multi.yml')
    db = SimulationDatabase('simulation.db', layout_config)
    print("KPI tables:", db.kpi_table)