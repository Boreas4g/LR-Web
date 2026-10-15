"""Optional Pinocchio kinematics owned by bookarm's arm stack."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Mapping, Optional, Sequence

import numpy as np

from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.devices.arm.protocol import DEFAULT_JOINT_LIMITS


class PinocchioKinematics:
    """FK/IK wrapper; the optional dependency is loaded only when motion needs it."""

    def __init__(
        self,
        urdf_path: Path,
        tcp_offset: Optional[Sequence[float]] = None,
        joint_limits: Optional[Mapping[int, Sequence[float]]] = None,
        inertia_config_path: Optional[Path] = None,
    ) -> None:
        try:
            import pinocchio as pin
        except ImportError as error:
            raise RuntimeError("cartesian arm motion requires the bookarm[kinematics] extra") from error
        self._pin = pin
        path = Path(urdf_path)
        if not path.is_file():
            raise FileNotFoundError("arm URDF not found: %s" % path)
        self._model = pin.buildModelFromUrdf(str(path))
        self._data = pin.Data(self._model)
        if inertia_config_path is not None:
            self._apply_calibrated_inertia(Path(inertia_config_path))
        self._frame_id = self._model.getFrameId("end_effector")
        if self._frame_id >= self._model.nframes:
            raise ValueError("arm URDF does not define end_effector frame")
        self._tcp = self._make_tcp(tcp_offset)
        self._joint_limits = joint_limits or DEFAULT_JOINT_LIMITS

    def _apply_calibrated_inertia(self, config_path: Path) -> None:
        """Override URDF link masses/levers with field-calibrated values.

        Mirrors the GUI SDK's loader (el_a3_sdk): the fitted values track the
        real arm far better than the nominal URDF, so gravity feedforward stops
        sagging and fighting the PD loop.
        """

        import sys

        try:
            import yaml
        except ImportError:
            print("[WARN] PyYAML unavailable; skipping inertia calibration", file=sys.stderr)
            return
        if not config_path.is_file():
            print(
                "[WARN] arm inertia calibration not found: %s" % config_path,
                file=sys.stderr,
            )
            return
        with config_path.open("r", encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle)
        if not cfg or not cfg.get("use_calibrated_params", False):
            return
        params = cfg.get("inertia_params", {})
        if not isinstance(params, Mapping):
            return
        # Same body-index map as the GUI SDK: L2..L6 -> bodies 2..6.
        link_map = {"L2": 2, "L3": 3, "L4": 4, "L5": 5, "L6": 6}
        applied = 0
        for link_name, body_idx in link_map.items():
            entry = params.get(link_name)
            if not isinstance(entry, Mapping) or body_idx >= self._model.nbodies:
                continue
            mass = entry.get("mass")
            com = entry.get("com")
            if mass is not None:
                self._model.inertias[body_idx].mass = float(mass)
            if com is not None and len(com) == 3:
                self._model.inertias[body_idx].lever = np.asarray(com, dtype=float)
            applied += 1
        if applied:
            self._data = self._pin.Data(self._model)

    def solve_ik(
        self,
        target: BasePose,
        seed: JointPose,
        max_iter: int = 200,
        eps: float = 1e-4,
        dt: float = 0.1,
        damping: float = 1e-6,
        joint_limit_margin: float = 0.05,
        interrupt_event: Optional[threading.Event] = None,
        fallback_seeds: Optional[Sequence[JointPose]] = None,
    ) -> JointPose:
        """Solve IK with the GUI SDK's constrained DLS configuration.

        ``inverse_kinematics`` mirrors the GUI SDK's optional-result API.  This
        wrapper preserves BookArm's existing exception-based service contract.
        The DLS iteration is sensitive to the seed pose: slightly different
        starting joints can drive it into a joint limit on an otherwise
        solvable target. When the primary seed fails, ``fallback_seeds`` are
        retried in order before giving up, without changing the iteration
        itself.
        """

        solution = self.inverse_kinematics(
            target,
            q_init=seed,
            max_iter=max_iter,
            eps=eps,
            dt=dt,
            damping=damping,
            joint_limit_margin=joint_limit_margin,
            interrupt_event=interrupt_event,
        )
        for fallback in fallback_seeds or ():
            if solution is not None:
                break
            solution = self.inverse_kinematics(
                target,
                q_init=fallback,
                max_iter=max_iter,
                eps=eps,
                dt=dt,
                damping=damping,
                joint_limit_margin=joint_limit_margin,
                interrupt_event=interrupt_event,
            )
        if solution is None:
            raise RuntimeError("arm inverse kinematics did not converge")
        return solution

    def inverse_kinematics(
        self,
        target: BasePose,
        q_init: Optional[JointPose | Sequence[float]] = None,
        max_iter: int = 200,
        eps: float = 1e-4,
        dt: float = 0.1,
        damping: float = 1e-6,
        joint_limit_margin: float = 0.05,
        interrupt_event: Optional[threading.Event] = None,
    ) -> Optional[JointPose]:
        """Return the GUI-equivalent constrained DLS IK result, or ``None``."""

        pin = self._pin
        q_values = q_init.values_rad if isinstance(q_init, JointPose) else q_init
        q = self._as_q(q_values) if q_values is not None else pin.neutral(self._model)
        desired = self._pose_to_se3(target) * self._tcp.inverse()
        error = None
        for _ in range(max(1, int(max_iter))):
            if interrupt_event is not None and interrupt_event.is_set():
                return None
            pin.forwardKinematics(self._model, self._data, q)
            pin.updateFramePlacements(self._model, self._data)
            error = pin.log6(self._data.oMf[self._frame_id].actInv(desired)).vector
            if float(np.linalg.norm(error)) < float(eps):
                return JointPose.from_values(q[:6])
            jacobian = pin.computeFrameJacobian(self._model, self._data, q, self._frame_id, pin.LOCAL)
            jtj = jacobian.T @ jacobian + float(damping) * np.eye(self._model.nv)
            delta = np.linalg.solve(jtj, jacobian.T @ error)
            q = pin.integrate(self._model, q, delta * float(dt))
            self._clamp_to_limits(q, joint_limit_margin)
        return None

    def ik_step(
        self,
        target: BasePose,
        current: JointPose | Sequence[float],
        damping: float = 5e-3,
        max_step: float = 0.5,
        max_iter: int = 3,
        converge_eps: float = 1e-5,
    ) -> tuple[JointPose, float]:
        """Perform the GUI SDK's bounded, singularity-aware realtime IK step."""

        pin = self._pin
        values = current.values_rad if isinstance(current, JointPose) else current
        q = self._as_q(values)
        desired = self._pose_to_se3(target) * self._tcp.inverse()
        error_norm = 0.0

        for _ in range(max(1, int(max_iter))):
            pin.forwardKinematics(self._model, self._data, q)
            pin.updateFramePlacements(self._model, self._data)
            error = pin.log6(self._data.oMf[self._frame_id].actInv(desired)).vector
            error_norm = float(np.linalg.norm(error))
            if error_norm < float(converge_eps):
                break

            jacobian = pin.computeFrameJacobian(self._model, self._data, q, self._frame_id, pin.LOCAL)
            singular_values = np.linalg.svd(jacobian, compute_uv=False)
            smallest, largest = float(singular_values[-1]), float(singular_values[0])
            adaptive_damping = float(damping)
            singular_threshold = 0.035
            if smallest < singular_threshold:
                ratio = (smallest / singular_threshold) ** 2
                adaptive_damping = max(adaptive_damping, float(damping) + 0.08 * (1.0 - ratio))
            condition = largest / max(smallest, 1e-12)
            if condition > 80.0:
                ratio = min((condition - 80.0) / 80.0, 1.0)
                adaptive_damping = max(adaptive_damping, float(damping) + 0.02 * ratio)

            jtj = jacobian.T @ jacobian + adaptive_damping * np.eye(self._model.nv)
            rhs = jacobian.T @ error
            fifth_joint_index = 4
            fifth_joint_safe = 0.1
            fifth_joint = float(q[fifth_joint_index])
            if abs(fifth_joint) < fifth_joint_safe and smallest < singular_threshold:
                gradient = np.zeros(self._model.nv)
                sign = 1.0 if fifth_joint >= 0.0 else -1.0
                gradient[fifth_joint_index] = 0.5 * (fifth_joint_safe * sign - fifth_joint)
                rhs += adaptive_damping * gradient

            delta = np.linalg.solve(jtj, rhs)
            delta_norm = float(np.linalg.norm(delta))
            if delta_norm > float(max_step):
                delta *= float(max_step) / delta_norm
            q = pin.integrate(self._model, q, delta)
            self._clamp_to_limits(q)

        return JointPose.from_values(q[:6]), error_norm

    def forward_kinematics(self, joints: JointPose) -> BasePose:
        pin = self._pin
        q = self._as_q(joints.values_rad)
        pin.forwardKinematics(self._model, self._data, q)
        pin.updateFramePlacements(self._model, self._data)
        transform = self._data.oMf[self._frame_id] * self._tcp
        rpy = pin.rpy.matrixToRpy(transform.rotation)
        return BasePose(
            float(transform.translation[0]), float(transform.translation[1]), float(transform.translation[2]),
            float(rpy[0]), float(rpy[1]), float(rpy[2]),
        )

    def compute_gravity(self, joints: JointPose | Sequence[float]) -> tuple[float, ...]:
        """Return Pinocchio RNEA gravity torques in logical joint coordinates."""

        values = joints.values_rad if isinstance(joints, JointPose) else joints
        q = self._as_q(values)
        zeros = np.zeros(self._model.nv)
        torques = self._pin.rnea(self._model, self._data, q, zeros, zeros)
        return tuple(float(value) for value in torques[:6])

    def compute_jacobian(self, joints: JointPose | Sequence[float]) -> np.ndarray:
        """Return the GUI SDK-equivalent world-aligned Jacobian at the TCP."""

        pin = self._pin
        values = joints.values_rad if isinstance(joints, JointPose) else joints
        q = self._as_q(values)
        pin.forwardKinematics(self._model, self._data, q)
        pin.updateFramePlacements(self._model, self._data)
        frame = self._data.oMf[self._frame_id]
        jacobian = pin.computeFrameJacobian(
            self._model, self._data, q, self._frame_id, pin.LOCAL_WORLD_ALIGNED,
        )
        offset_world = frame.rotation @ self._tcp.translation
        tcp_jacobian = jacobian.copy()
        tcp_jacobian[:3, :] -= self._skew(offset_world) @ tcp_jacobian[3:, :]
        return tcp_jacobian

    def _as_q(self, values: Sequence[float]):
        q = np.zeros(self._model.nq)
        q[:6] = list(values)[:6]
        return q

    def _make_tcp(self, values: Optional[Sequence[float]]):
        if values is None:
            return self._pin.SE3.Identity()
        if len(values) != 6:
            raise ValueError("TCP offset must contain six values")
        return self._pin.SE3(
            self._pin.rpy.rpyToMatrix(*[float(value) for value in values[3:]]),
            np.asarray(values[:3], dtype=float),
        )

    def _pose_to_se3(self, pose: BasePose):
        return self._pin.SE3(
            self._pin.rpy.rpyToMatrix(pose.rx_rad, pose.ry_rad, pose.rz_rad),
            np.asarray((pose.x_m, pose.y_m, pose.z_m), dtype=float),
        )

    def _clamp_to_limits(self, q: np.ndarray, margin: float = 0.0) -> None:
        """Keep IK iterates in the configured joint workspace."""

        for index in range(min(6, len(q))):
            lower, upper = self._joint_limits[index + 1]
            q[index] = np.clip(float(q[index]), float(lower) + margin, float(upper) - margin)

    @staticmethod
    def _skew(vector: np.ndarray) -> np.ndarray:
        x, y, z = (float(value) for value in vector)
        return np.asarray(((0.0, -z, y), (z, 0.0, -x), (-y, x, 0.0)), dtype=float)
