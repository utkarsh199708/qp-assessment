package com.grocery.booking.grocery.DTO;

import jakarta.persistence.Entity;
import lombok.Data;


@Data
public class InventoryManagementRequest {
    private String operation;
    private int quantity;


}