defmodule Dispatch.Job do
  @enforce_keys [:id, :state]
  defstruct [:id, :state]

  def from_wire(%{"id" => id, "state" => state}) when is_binary(id) do
    case state do
      "queued" -> {:ok, %__MODULE__{id: id, state: :queued}}
      "running" -> {:ok, %__MODULE__{id: id, state: :running}}
      _ -> {:error, :invalid_state}
    end
  end

  def to_wire(%__MODULE__{id: id, state: :queued}) do
    %{"id" => id, "state" => "queued"}
  end

  def next_state("queued"), do: "running"
end
